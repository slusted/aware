"""HTTP surface for the standalone Predicates library page.

Predicates were originally surfaced as a tab on /scenarios. As the
sidebar IA evolved into FINDINGS / PRESENT / FUTURE / EXECUTE / SYSTEM,
predicates moved under FINDINGS as a first-class concept (the abstraction
layer between raw evidence and everything else). This route renders the
same data the Scenarios "Predicates" tab uses — same service layer, same
card partial — at a dedicated URL.

Drill-through (predicate_key -> full detail page) still lives at
/scenarios/predicates/{predicate_key}; the card partial links there
directly so we don't need to relocate the detail route in this phase.

Phase 3a: predicates carry a `source` field ('user' | 'llm_proposed' |
'llm_promoted'). The page accepts a `?source=` filter and exposes
two API endpoints (promote / reject) so reviewers can act on
LLM-proposed predicates as soon as the suggestion job (3b) starts
producing them. Until then the proposed filter shows an empty state.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..deps import get_current_user, get_db, require_role
from ..models import Predicate, User
from ..scenarios import dashboard as dashboard_svc


VALID_PREDICATE_SORTS = ("shifting", "contested", "alpha", "category")
VALID_SOURCES = ("user", "llm_proposed", "llm_promoted")


router = APIRouter(tags=["predicates"], include_in_schema=False)
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)


def _coerce_sort(value: str | None, allowed: tuple[str, ...], default: str) -> str:
    if not value or value not in allowed:
        return default
    return value


def _coerce_source(value: str | None) -> str | None:
    """`source=all` and missing → None (no filter). Unknown values fall
    back to None so a stale URL doesn't 500."""
    if not value or value == "all":
        return None
    return value if value in VALID_SOURCES else None


@router.get("/predicates", response_class=HTMLResponse)
def predicates_index(
    request: Request,
    sort: str = "shifting",
    category: str | None = None,
    only_no_recent: int = 0,
    source: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sort = _coerce_sort(sort, VALID_PREDICATE_SORTS, "shifting")
    source_filter = _coerce_source(source)

    all_summaries = dashboard_svc.predicate_summary(db)
    summaries = list(all_summaries)
    if source_filter:
        summaries = [s for s in summaries if s.source == source_filter]
    if category:
        summaries = [s for s in summaries if s.category == category]
    if only_no_recent:
        summaries = [s for s in summaries if not s.has_recent_evidence]
    summaries = dashboard_svc.sort_summaries(summaries, sort)

    categories = sorted({s.category for s in all_summaries})

    # Counts per source — drives the filter pill labels and the future
    # sidebar review-queue badge. Computed off the unfiltered list so
    # selecting one filter doesn't make the others vanish.
    source_counts = {key: 0 for key in VALID_SOURCES}
    for s in all_summaries:
        if s.source in source_counts:
            source_counts[s.source] += 1

    return templates.TemplateResponse(request, "predicates_index.html", {
        "user": user,
        "sort": sort,
        "category": category,
        "categories": categories,
        "only_no_recent": bool(only_no_recent),
        "source": source_filter or "all",
        "source_counts": source_counts,
        "summaries": summaries,
        "header": dashboard_svc.header_counts(db),
        "valid_predicate_sorts": VALID_PREDICATE_SORTS,
    })


@router.post("/api/predicates/{predicate_key}/promote")
def promote_predicate(
    predicate_key: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst", "admin")),
):
    """Accept an LLM-proposed predicate. Flips source to 'llm_promoted'
    so it stops appearing in the review queue but its provenance stays
    visible. No-op (200) if already promoted; 400 if the predicate was
    user-authored (nothing to promote)."""
    p = db.query(Predicate).filter(Predicate.key == predicate_key).first()
    if not p:
        raise HTTPException(404, f"predicate {predicate_key!r} not found")
    if p.source == "user":
        raise HTTPException(
            400, f"predicate {predicate_key!r} was user-authored — nothing to promote"
        )
    if p.source != "llm_promoted":
        p.source = "llm_promoted"
        p.updated_at = datetime.utcnow()
        db.commit()
    return JSONResponse({"key": p.key, "source": p.source})


@router.post("/api/predicates/{predicate_key}/reject")
def reject_predicate(
    predicate_key: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst", "admin")),
):
    """Reject a predicate (typically LLM-proposed). Marks it inactive so
    it stops contributing to scenario derivation and disappears from
    the default /predicates view; the row + its provenance metadata
    remain for audit. To undo, edit the predicate from the detail page."""
    p = db.query(Predicate).filter(Predicate.key == predicate_key).first()
    if not p:
        raise HTTPException(404, f"predicate {predicate_key!r} not found")
    if p.active:
        p.active = False
        p.updated_at = datetime.utcnow()
        db.commit()
    return JSONResponse({"key": p.key, "active": p.active})
