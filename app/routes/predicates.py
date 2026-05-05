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
producing them.

Phase 3b: adds the LLM-suggestion job. Manual trigger via
POST /api/runs/predicate-proposal; sidebar review-queue badge fed by
GET /partials/predicate-proposed-count.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import jobs
from ..deps import get_current_user, get_db, require_role
from ..models import Predicate, User
from ..scenarios import dashboard as dashboard_svc
from ..scenarios import review as review_svc


VALID_PREDICATE_SORTS = ("needs_review", "category", "alpha")
VALID_SOURCES = ("user", "llm_proposed", "llm_promoted")


router = APIRouter(tags=["predicates"], include_in_schema=False)
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)
# Re-register the agent_brand Jinja global on this route's env. Missed
# from #109 — every Templates instance needs this so base.html's
# `{{ agent_brand.name }}` / `{{ agent_brand.avatar_url }}` resolve.
from .. import agent_brand as _agent_brand
_agent_brand.register_template_globals(templates)


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
    sort: str = "needs_review",
    category: str | None = None,
    only_no_recent: int = 0,
    source: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sort = _coerce_sort(sort, VALID_PREDICATE_SORTS, "needs_review")
    source_filter = _coerce_source(source)

    all_summaries = dashboard_svc.predicate_summary(db)
    summaries = list(all_summaries)
    if source_filter:
        summaries = [s for s in summaries if s.source == source_filter]
    if category:
        summaries = [s for s in summaries if s.category == category]
    if only_no_recent:
        summaries = [s for s in summaries if not s.has_recent_evidence]

    # Pending-proposal counts per predicate — drives the "needs review"
    # chip on cards and the "Needs review" sort. One query, grouped here
    # rather than per-card.
    pending_by_key: dict[str, int] = {}
    for prop in review_svc.pending_proposals_for(db, None):
        if prop.source_predicate_key:
            pending_by_key[prop.source_predicate_key] = (
                pending_by_key.get(prop.source_predicate_key, 0) + 1
            )

    if sort == "needs_review":
        summaries = sorted(
            summaries,
            key=lambda s: (-pending_by_key.get(s.key, 0), s.category, s.key),
        )
    else:
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
        "pending_by_key": pending_by_key,
        "pending_proposal_total": sum(pending_by_key.values()),
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


# ── Phase 3b: proposer trigger + sidebar badge ──────────────────────────

@router.post("/api/runs/predicate-proposal", status_code=202)
def trigger_predicate_proposal(
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst", "admin")),
):
    """Manually enqueue a predicate-proposal run. Drainer picks it up,
    the LLM proposer scans recent findings + the current roster, and
    writes any new predicates worth tracking with source='llm_proposed'.
    They land in /predicates?source=llm_proposed for review."""
    run = jobs.enqueue_run(
        db,
        "predicate_proposal",
        triggered_by="manual",
    )
    return {
        "queued": True,
        "kind": "predicate_proposal",
        "run_id": run.id,
        "queue_position": jobs.queue_position(db, run.id),
    }


@router.get("/partials/predicate-proposed-count", response_class=HTMLResponse)
def predicate_proposed_count_partial(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Tiny HTMX partial — renders just the review-queue badge.
    Hooked from the sidebar Predicates entry. Hidden when zero so the
    sidebar stays uncluttered."""
    n = (
        db.query(Predicate)
        .filter(Predicate.active.is_(True))
        .filter(Predicate.source == "llm_proposed")
        .count()
    )
    if n <= 0:
        return HTMLResponse("")
    return HTMLResponse(
        f'<span class="nav-badge" title="Proposed predicates awaiting review">{n}</span>'
    )
