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
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..deps import get_current_user, get_db
from ..models import User
from ..scenarios import dashboard as dashboard_svc


VALID_PREDICATE_SORTS = ("shifting", "contested", "alpha", "category")


router = APIRouter(tags=["predicates"], include_in_schema=False)
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)


def _coerce_sort(value: str | None, allowed: tuple[str, ...], default: str) -> str:
    if not value or value not in allowed:
        return default
    return value


@router.get("/predicates", response_class=HTMLResponse)
def predicates_index(
    request: Request,
    sort: str = "shifting",
    category: str | None = None,
    only_no_recent: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sort = _coerce_sort(sort, VALID_PREDICATE_SORTS, "shifting")

    summaries = dashboard_svc.predicate_summary(db)
    if category:
        summaries = [s for s in summaries if s.category == category]
    if only_no_recent:
        summaries = [s for s in summaries if not s.has_recent_evidence]
    summaries = dashboard_svc.sort_summaries(summaries, sort)

    categories = sorted({s.category for s in dashboard_svc.predicate_summary(db)})

    return templates.TemplateResponse(request, "predicates_index.html", {
        "user": user,
        "sort": sort,
        "category": category,
        "categories": categories,
        "only_no_recent": bool(only_no_recent),
        "summaries": summaries,
        "header": dashboard_svc.header_counts(db),
        "valid_predicate_sorts": VALID_PREDICATE_SORTS,
    })
