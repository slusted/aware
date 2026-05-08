"""Public, unauthenticated routes (docs/stream/01-public-share-link.md).

Currently a single endpoint: GET /p/{token} renders a read-only view of a
SavedFilter that the owner has explicitly shared. No login, no per-user
state, no swipe / pin / dismiss / debug chrome.

Lives in its own module (rather than inside `filters.py`) because:
  - The prefix is `/p`, not `/api/filters`.
  - It must NOT depend on `get_current_user` — adding it here keeps the
    "no auth dependency" property obvious by inspection.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import public_qa
from ..deps import get_db
from ..models import SavedFilter
from ..ui import _build_logo_map, _parse_stream_filters, _public_stream_query, register_signal_globals as _register_signal_globals


router = APIRouter(tags=["public"])
# Match the absolute-path pattern used by app/ui.py and app/routes/auth.py so
# template discovery doesn't depend on the process CWD.
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
# `_stream_card.html` looks up signal_type labels via this global; without
# the registration the badge would render the raw enum value.
_register_signal_globals(templates)
_register_signal_globals(templates)


@router.get("/p/{token}", response_class=HTMLResponse)
def public_stream(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Render a public read-only view of a shared saved filter.

    404 covers three cases that look identical to the viewer (deliberate —
    the same response for "never existed", "revoked", and "deleted"
    avoids leaking whether a token was ever valid):
      - Unknown token.
      - Filter row deleted (token rows null'd by ORM cascade in practice).
      - Token explicitly revoked (column nulled).
    """
    sf = (
        db.query(SavedFilter)
        .filter(SavedFilter.public_token == token)
        .first()
    )
    if sf is None or not sf.public_token:
        raise HTTPException(404, "this share link is no longer active")

    # Parse the saved spec through the same helper the authenticated stream
    # uses so behavior stays in lockstep. `?explain=` is not honoured on
    # the public route — drop it deliberately by stripping the key from
    # whatever the spec might contain.
    spec = dict(sf.spec or {})
    spec.pop("explain", None)
    filters = _parse_stream_filters(spec)
    findings, has_more = _public_stream_query(db, filters)

    response = templates.TemplateResponse(request, "public_stream.html", {
        "filter_name": sf.name,
        "findings": findings,
        "has_more": has_more,
        "logos": _build_logo_map(db, findings),
        "now": datetime.utcnow(),
        # Owner-controlled toggle. The Q&A panel + /ask endpoint only
        # mount when this is true; default-false shares render exactly
        # as before.
        "qa_enabled": bool(getattr(sf, "public_qa_enabled", False)),
        "share_token": token,
    })
    # Defence in depth against indexers / link unfurlers crawling the page.
    # The <meta> tag inside the template covers HTML-aware crawlers; this
    # header covers everything else (Slackbot, Twitterbot, etc).
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# ---- Public Q&A agent (docs/stream/01-public-share-link.md follow-up) ------
#
# The Q&A panel is a separate, opt-in surface on top of the read-only share.
# Endpoints:
#   POST /p/{token}/ask         — stream a model answer over SSE.
#   GET  /p/{token}/ask/budget  — current per-share budget remaining.
# Both 404 with the same generic message used by the page render so we don't
# leak the presence of revoked tokens. Both 403 when the owner has not
# enabled Q&A on this share.


class _AskBody(BaseModel):
    question: str


def _resolve_share(db: Session, token: str) -> SavedFilter:
    """Resolve a token to an active share row, or raise the same 404 the
    page render uses. Centralised so /ask + /budget can't drift on the
    "what counts as a live share" question."""
    sf = (
        db.query(SavedFilter)
        .filter(SavedFilter.public_token == token)
        .first()
    )
    if sf is None or not sf.public_token:
        raise HTTPException(404, "this share link is no longer active")
    return sf


def _findings_for_share(db: Session, sf: SavedFilter):
    spec = dict(sf.spec or {})
    spec.pop("explain", None)
    filters = _parse_stream_filters(spec)
    findings, _ = _public_stream_query(db, filters)
    return findings


@router.post("/p/{token}/ask")
def public_ask(
    token: str,
    body: _AskBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Answer one question about the findings on this share. Streams SSE.

    Auth shape: no login, but the owner must have flipped public_qa_enabled
    on the SavedFilter. We intentionally use a 403 (not 404) for "Q&A is
    off" so the client can show a helpful message — the share itself is
    still public, just the Q&A panel isn't.
    """
    sf = _resolve_share(db, token)
    if not getattr(sf, "public_qa_enabled", False):
        raise HTTPException(403, "Q&A is not enabled on this share.")
    findings = _findings_for_share(db, sf)
    ip = request.client.host if request.client else None
    return StreamingResponse(
        public_qa.stream_answer(db, sf, findings, body.question, ip=ip),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@router.get("/p/{token}/ask/budget")
def public_ask_budget(
    token: str,
    db: Session = Depends(get_db),
):
    """Read the share's remaining daily budget. Used by the panel to show
    "X questions left today" without spending a model call.

    Mirrors /ask's gating: 404 on revoked tokens, 403 when Q&A is off.
    Doesn't touch the IP bucket — checking remaining shouldn't burn a slot.
    """
    sf = _resolve_share(db, token)
    if not getattr(sf, "public_qa_enabled", False):
        raise HTTPException(403, "Q&A is not enabled on this share.")
    return JSONResponse(public_qa.remaining_for_share(db, sf.id))
