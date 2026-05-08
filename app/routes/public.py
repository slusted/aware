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
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

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
    })
    # Defence in depth against indexers / link unfurlers crawling the page.
    # The <meta> tag inside the template covers HTML-aware crawlers; this
    # header covers everything else (Slackbot, Twitterbot, etc).
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response
