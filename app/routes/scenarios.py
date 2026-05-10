"""HTTP surface for in-card predicate tagging
(docs/scenarios/02-card-tagging.md).

Stage 2: every Finding card on /stream gets a small predicate-tagging
affordance. Flipping the card opens the form below; LLM proposals
(written by the classifier sweep) come pre-filled and require one
click to confirm. Manual additions go through the same form.

All mutations trigger a synchronous single-predicate recompute so the
posterior moves the moment evidence is committed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import jobs
from ..deps import get_current_user, get_db, require_role
from ..models import (
    Finding,
    Predicate,
    PredicateState,
    PredicateEvidence,
    Scenario,
    ScenarioPredicateLink,
    SignalView,
    User,
)
from ..scenarios import dashboard as dashboard_svc
from ..scenarios import review as review_svc
from ..scenarios.service import (
    accept_proposal as svc_accept_proposal,
    recompute_predicate,
    reject_proposal as svc_reject_proposal,
    update_predicate as svc_update_predicate,
    update_scenario as svc_update_scenario,
)


router = APIRouter(tags=["scenarios"], include_in_schema=False)
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)
from .. import agent_brand as _agent_brand
_agent_brand.register_template_globals(templates)
from ..ui import register_signal_globals as _register_signal_globals
_register_signal_globals(templates)

# Surface the per-evidence cap in templates so the math view can show
# users the exact bound their posteriors were computed against.
from ..scenarios.posterior import MAX_ABS_LOGIT_DELTA as _SCENARIOS_MAX_LOGIT
templates.env.globals["scenarios_max_logit_delta"] = _SCENARIOS_MAX_LOGIT


VALID_DIRECTIONS = ("support", "contradict", "neutral")
VALID_STRENGTHS = ("weak", "moderate", "strong")
# Soft cap enforced in the form; the schema accepts more so a future
# change of mind is one-line.
MAX_EVIDENCE_PER_FINDING = 2


# ── Card / form context helpers ─────────────────────────────────────────

def _evidence_for_finding(db: Session, finding_id: int) -> list[PredicateEvidence]:
    """All evidence rows for one finding, oldest-first so the form lists
    them in creation order. Excludes nothing — rejected rows still
    render (greyed) so the operator can un-reject if they change their
    mind."""
    return (
        db.query(PredicateEvidence)
        .filter(PredicateEvidence.finding_id == finding_id)
        .order_by(PredicateEvidence.id.asc())
        .all()
    )


def _predicates_with_states(db: Session) -> list[dict]:
    """Roster shape the form template renders into the predicate +
    dependent-state dropdowns. Active only — inactive predicates can't
    be tagged against."""
    pred_rows = (
        db.query(Predicate)
        .filter(Predicate.active.is_(True))
        .order_by(Predicate.id)
        .all()
    )
    states_by_pred: dict[int, list[PredicateState]] = {}
    for s in db.query(PredicateState).all():
        states_by_pred.setdefault(s.predicate_id, []).append(s)

    out = []
    for p in pred_rows:
        states = sorted(
            states_by_pred.get(p.id, []),
            key=lambda s: s.ordinal_position,
        )
        out.append({
            "key": p.key,
            "name": p.name,
            "category": p.category,
            "states": [
                {"state_key": s.state_key, "label": s.label}
                for s in states
            ],
        })
    return out


def _predicate_label_lookup(db: Session) -> dict[tuple[str, str], tuple[str, str]]:
    """{(predicate_key, state_key): (predicate_name, state_label)} so the
    badge / row renderers can show human labels without a per-row query."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    pred_id_to_key = {}
    pred_id_to_name = {}
    for p in db.query(Predicate).all():
        pred_id_to_key[p.id] = p.key
        pred_id_to_name[p.id] = p.name
    for s in db.query(PredicateState).all():
        pkey = pred_id_to_key.get(s.predicate_id)
        if pkey is None:
            continue
        out[(pkey, s.state_key)] = (
            pred_id_to_name.get(s.predicate_id, pkey),
            s.label,
        )
    return out


def _evidence_to_view(
    ev: PredicateEvidence,
    pred_id_to_key: dict[int, str],
    label_lookup: dict[tuple[str, str], tuple[str, str]],
) -> dict:
    """Flatten a PredicateEvidence row into the dict the template wants."""
    pkey = pred_id_to_key.get(ev.predicate_id, "?")
    pname, slabel = label_lookup.get((pkey, ev.target_state_key), (pkey, ev.target_state_key))
    return {
        "id": ev.id,
        "predicate_key": pkey,
        "predicate_name": pname,
        "target_state_key": ev.target_state_key,
        "target_state_label": slabel,
        "direction": ev.direction,
        "strength_bucket": ev.strength_bucket,
        "credibility": ev.credibility,
        "classified_by": ev.classified_by,
        "confirmed_at": ev.confirmed_at,
        "notes": ev.notes,
    }


def _build_card_context(db: Session, f: Finding, user: User | None) -> dict:
    """Common context the re-rendered _stream_card.html partial needs.
    Fetched fresh after each mutation so the badge updates in place."""
    evidence_rows = _evidence_for_finding(db, f.id)
    pred_id_to_key = {p.id: p.key for p in db.query(Predicate).all()}
    label_lookup = _predicate_label_lookup(db)
    f._predicate_evidence = [
        _evidence_to_view(ev, pred_id_to_key, label_lookup)
        for ev in evidence_rows
    ]
    view = None
    if user is not None:
        view = (
            db.query(SignalView)
            .filter(
                SignalView.user_id == user.id,
                SignalView.finding_id == f.id,
            )
            .first()
        )
    # Defer logos to the existing helper in app/ui.py if needed; the
    # partial degrades cleanly when logos is missing (placeholder span).
    return {"f": f, "view": view, "logos": {}}


def _render_card(request: Request, db: Session, f: Finding, user: User) -> HTMLResponse:
    """Re-render the card partial with fresh evidence. Used by every
    mutation route so the front-of-card badge updates immediately after
    HTMX swap."""
    ctx = _build_card_context(db, f, user)
    return templates.TemplateResponse(request, "_stream_card.html", ctx)


# ── Form GET ────────────────────────────────────────────────────────────

@router.get(
    "/partials/finding/{finding_id}/predicate_form",
    response_class=HTMLResponse,
)
def predicate_form(
    finding_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """HTMX partial: the back-of-card form, pre-filled with existing
    evidence rows + a ready-to-add slot if we're under the soft cap."""
    f = db.get(Finding, finding_id)
    if f is None:
        raise HTTPException(404, "finding not found")

    evidence_rows = _evidence_for_finding(db, finding_id)
    pred_id_to_key = {p.id: p.key for p in db.query(Predicate).all()}
    label_lookup = _predicate_label_lookup(db)
    rows_view = [
        _evidence_to_view(ev, pred_id_to_key, label_lookup)
        for ev in evidence_rows
    ]
    # Active rows = anything except user_rejected. The soft cap is
    # against active rows so a rejected proposal doesn't burn the slot.
    active_count = sum(
        1 for r in rows_view if r["classified_by"] != "user_rejected"
    )
    view = (
        db.query(SignalView)
        .filter(
            SignalView.user_id == user.id,
            SignalView.finding_id == finding_id,
        )
        .first()
    )
    return templates.TemplateResponse(request, "_predicate_form.html", {
        "f": f,
        "evidence_rows": rows_view,
        "predicates": _predicates_with_states(db),
        "directions": VALID_DIRECTIONS,
        "strengths": VALID_STRENGTHS,
        "max_per_finding": MAX_EVIDENCE_PER_FINDING,
        "can_add_more": active_count < MAX_EVIDENCE_PER_FINDING,
        "view_state": view.state if view else None,
    })


@router.get("/partials/predicate_states", response_class=HTMLResponse)
def predicate_states(
    request: Request,
    predicate_key: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Tiny HTMX endpoint: the set of <option> elements for the chosen
    predicate. The form's state dropdown swaps to this when the
    predicate dropdown changes — keeps initial page weight down (we
    don't dump every state for every predicate into the form on first
    render).

    Query-param shape (rather than a path param) so HTMX can wire the
    select's `change` event straight to `hx-get` with `hx-vals` for the
    selected predicate_key — no JS substitution needed."""
    pred = db.query(Predicate).filter(Predicate.key == predicate_key).one_or_none()
    if pred is None:
        raise HTTPException(404, "prediction not found")
    states = (
        db.query(PredicateState)
        .filter(PredicateState.predicate_id == pred.id)
        .order_by(PredicateState.ordinal_position)
        .all()
    )
    return templates.TemplateResponse(request, "_predicate_state_options.html", {
        "predicate": pred,
        "states": states,
    })


# ── Mutations ───────────────────────────────────────────────────────────

def _validate_evidence_input(
    db: Session,
    predicate_key: str,
    target_state_key: str,
    direction: str,
    strength_bucket: str,
) -> Predicate:
    """Common validation shared by create / edit. Returns the resolved
    Predicate row."""
    if direction not in VALID_DIRECTIONS:
        raise HTTPException(400, f"direction must be one of {VALID_DIRECTIONS}")
    if strength_bucket not in VALID_STRENGTHS:
        raise HTTPException(400, f"strength_bucket must be one of {VALID_STRENGTHS}")
    pred = (
        db.query(Predicate)
        .filter(Predicate.key == predicate_key, Predicate.active.is_(True))
        .one_or_none()
    )
    if pred is None:
        raise HTTPException(400, f"unknown or inactive prediction {predicate_key!r}")
    state = (
        db.query(PredicateState)
        .filter(
            PredicateState.predicate_id == pred.id,
            PredicateState.state_key == target_state_key,
        )
        .one_or_none()
    )
    if state is None:
        raise HTTPException(
            400,
            f"unknown state {target_state_key!r} for prediction {predicate_key!r}",
        )
    return pred


@router.post(
    "/partials/finding/{finding_id}/evidence",
    response_class=HTMLResponse,
)
def create_evidence(
    finding_id: int,
    request: Request,
    predicate_key: str = Form(...),
    target_state_key: str = Form(...),
    direction: str = Form(...),
    strength_bucket: str = Form(...),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
):
    """Create one new manually-entered evidence row, pre-confirmed.
    Triggers a single-predicate recompute so the posterior moves
    before the card re-renders."""
    f = db.get(Finding, finding_id)
    if f is None:
        raise HTTPException(404, "finding not found")
    pred = _validate_evidence_input(
        db, predicate_key, target_state_key, direction, strength_bucket,
    )

    now = datetime.utcnow()
    observed_at = f.published_at or f.created_at or now
    ev = PredicateEvidence(
        finding_id=finding_id,
        predicate_id=pred.id,
        target_state_key=target_state_key,
        direction=direction,
        strength_bucket=strength_bucket,
        # Manual entries default to 1.0; classifier/CLI override per source.
        credibility=1.0,
        classified_by="manual",
        observed_at=observed_at,
        confirmed_at=now,
        notes=(notes or None),
    )
    db.add(ev)
    db.flush()
    recompute_predicate(db, pred.id, now=now)
    db.commit()
    return _render_card(request, db, f, user)


@router.post(
    "/partials/finding/{finding_id}/evidence/{evidence_id}/confirm",
    response_class=HTMLResponse,
)
def confirm_evidence(
    finding_id: int,
    evidence_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
):
    """Confirm an LLM-proposed row as-is. Stamps confirmed_at, leaves
    classified_by="llm" (the LLM was right). Triggers recompute."""
    f = db.get(Finding, finding_id)
    if f is None:
        raise HTTPException(404, "finding not found")
    ev = db.get(PredicateEvidence, evidence_id)
    if ev is None or ev.finding_id != finding_id:
        raise HTTPException(404, "evidence not found for this finding")

    now = datetime.utcnow()
    ev.confirmed_at = now
    db.flush()
    recompute_predicate(db, ev.predicate_id, now=now)
    db.commit()
    return _render_card(request, db, f, user)


@router.post(
    "/partials/finding/{finding_id}/evidence/{evidence_id}/edit",
    response_class=HTMLResponse,
)
def edit_evidence(
    finding_id: int,
    evidence_id: int,
    request: Request,
    predicate_key: str = Form(...),
    target_state_key: str = Form(...),
    direction: str = Form(...),
    strength_bucket: str = Form(...),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
):
    """Edit-in-place: update fields, stamp classified_by="user_override"
    if it was previously an LLM proposal so we capture the disagreement
    signal for prompt tuning later."""
    f = db.get(Finding, finding_id)
    if f is None:
        raise HTTPException(404, "finding not found")
    ev = db.get(PredicateEvidence, evidence_id)
    if ev is None or ev.finding_id != finding_id:
        raise HTTPException(404, "evidence not found for this finding")
    pred = _validate_evidence_input(
        db, predicate_key, target_state_key, direction, strength_bucket,
    )

    # If the user is editing an LLM proposal, mark as override. If
    # they're editing their own previous edit, keep it as user_override.
    # Pure manual rows stay manual.
    previous_predicate_id = ev.predicate_id
    if ev.classified_by == "llm":
        ev.classified_by = "user_override"

    now = datetime.utcnow()
    ev.predicate_id = pred.id
    ev.target_state_key = target_state_key
    ev.direction = direction
    ev.strength_bucket = strength_bucket
    ev.notes = (notes or None)
    ev.confirmed_at = now
    db.flush()

    # If the predicate changed, recompute both — the old one to drop
    # this evidence's contribution, the new one to pick it up.
    if previous_predicate_id != pred.id:
        recompute_predicate(db, previous_predicate_id, now=now)
    recompute_predicate(db, pred.id, now=now)
    db.commit()
    return _render_card(request, db, f, user)


@router.post(
    "/partials/finding/{finding_id}/evidence/{evidence_id}/reject",
    response_class=HTMLResponse,
)
def reject_evidence(
    finding_id: int,
    evidence_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
):
    """Soft-reject. classified_by="user_rejected", confirmed_at stays
    NULL. Recompute filter excludes the row. Audit trail preserved."""
    f = db.get(Finding, finding_id)
    if f is None:
        raise HTTPException(404, "finding not found")
    ev = db.get(PredicateEvidence, evidence_id)
    if ev is None or ev.finding_id != finding_id:
        raise HTTPException(404, "evidence not found for this finding")

    was_confirmed = ev.confirmed_at is not None
    ev.classified_by = "user_rejected"
    ev.confirmed_at = None
    db.flush()
    # Only recompute if this row was previously contributing to the
    # posterior (confirmed). Otherwise the math is already unchanged.
    if was_confirmed:
        recompute_predicate(db, ev.predicate_id, now=datetime.utcnow())
    db.commit()
    return _render_card(request, db, f, user)


# ── Manual sweep trigger (admin) ────────────────────────────────────────

@router.post("/api/runs/scenarios-classify-sweep", status_code=202)
def trigger_classify_sweep(
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Manually enqueue a classifier sweep. Useful after a predicate-set
    change or to backfill historical findings."""
    run = jobs.enqueue_run(
        db,
        "scenarios_classify_sweep",
        triggered_by="manual",
    )
    return {
        "queued": True,
        "kind": "scenarios_classify_sweep",
        "run_id": run.id,
        "queue_position": jobs.queue_position(db, run.id),
    }


# ── Stage 6: predicate review (docs/scenarios/06-predicate-review.md) ──


@router.post(
    "/scenarios/proposals/{proposal_id}/accept",
    response_class=HTMLResponse,
)
def proposal_accept(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
):
    """Accept one pending proposal — applies the change via service-layer
    update_predicate / evidence-mutation paths and marks the row
    `accepted`. Returns the re-rendered card partial so an HTMX swap
    shows the new status inline."""
    try:
        svc_accept_proposal(db, proposal_id, user)
        db.commit()
    except ValueError as e:
        db.rollback()
        return _render_proposal_card(
            request, db, proposal_id, error=str(e), status_code=400,
        )
    return _render_proposal_card(request, db, proposal_id)


@router.post(
    "/scenarios/proposals/{proposal_id}/reject",
    response_class=HTMLResponse,
)
async def proposal_reject(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
):
    """Reject one pending proposal. No mutation beyond the proposal row
    itself. Optional `reason` form field captured for the audit trail."""
    form = await request.form()
    reason = (form.get("reason") or "").strip() or None
    try:
        svc_reject_proposal(db, proposal_id, user, reason=reason)
        db.commit()
    except ValueError as e:
        db.rollback()
        return _render_proposal_card(
            request, db, proposal_id, error=str(e), status_code=400,
        )
    return _render_proposal_card(request, db, proposal_id)


def _render_proposal_card(
    request: Request,
    db: Session,
    proposal_id: int,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    proposal = review_svc.get_proposal_view(db, proposal_id)
    if proposal is None:
        raise HTTPException(404, f"proposal {proposal_id} not found")
    return templates.TemplateResponse(
        request,
        "_predicate_proposal_card.html",
        {"proposal": proposal, "error": error},
        status_code=status_code,
    )


@router.post(
    "/scenarios/predicates/{predicate_key}/dismiss-review",
    status_code=202,
)
def predicate_dismiss_review(
    predicate_key: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
):
    """"Looks good — dismiss for Nd" button. Bumps next_review_due_at
    so the monthly job skips this predicate until the cooldown elapses.
    N comes from `predicate_review_dismiss_days` setting (default 30)."""
    pred = (
        db.query(Predicate).filter(Predicate.key == predicate_key).one_or_none()
    )
    if pred is None:
        raise HTTPException(404, f"prediction {predicate_key!r} not found")
    days = review_svc.dismiss_days(db)
    pred.next_review_due_at = datetime.utcnow() + timedelta(days=days)
    db.commit()
    return {"dismissed_until": pred.next_review_due_at.isoformat(), "days": days}


@router.post("/api/runs/predicate-review", status_code=202)
def trigger_predicate_review(
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
):
    """Manually enqueue a predicate-review run. Mirrors the recompute
    pattern. Body-less POST — the agent / dashboard digest button just
    needs to fire-and-forget."""
    run = jobs.enqueue_run(
        db,
        "predicate_review",
        triggered_by="manual",
    )
    return {
        "queued": True,
        "kind": "predicate_review",
        "run_id": run.id,
        "queue_position": jobs.queue_position(db, run.id),
    }


@router.post("/api/runs/scenarios-recompute", status_code=202)
def trigger_recompute(
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
):
    """Manually enqueue a posterior recompute. Useful when likelihood
    ratios or decay settings have been edited via SQL — the dashboard
    won't reflect the new math until a recompute runs against the
    existing evidence."""
    run = jobs.enqueue_run(
        db,
        "scenarios_recompute",
        triggered_by="manual",
    )
    return {
        "queued": True,
        "kind": "scenarios_recompute",
        "run_id": run.id,
        "queue_position": jobs.queue_position(db, run.id),
    }


# ── Stage-3: dashboard ──────────────────────────────────────────────────

VALID_PREDICATE_SORTS = ("shifting", "contested", "alpha", "category")
VALID_EVIDENCE_SORTS = (
    "observed_desc", "observed_asc", "confirmed_desc",
    "credibility_desc", "contribution_desc",
)


def _coerce_sort(value: str | None, allowed: tuple[str, ...], default: str) -> str:
    if not value or value not in allowed:
        return default
    return value


@router.get("/scenarios", response_class=HTMLResponse)
def scenarios_index(
    request: Request,
    tab: str = "predicates",
    sort: str = "shifting",
    category: str | None = None,
    only_no_recent: int = 0,
    evidence_sort: str = "observed_desc",
    evidence_offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Main /scenarios page. Read-only this stage. Tabs:
      - predicates (default): grid of cards, sortable, filterable
      - evidence: flat confirmed evidence table
      - scenarios / settings: reserved (stage 4 / 5), 404 inside the
        template via a placeholder panel — no separate route needed.
    """
    if tab not in ("predicates", "evidence", "scenarios", "settings"):
        tab = "predicates"
    sort = _coerce_sort(sort, VALID_PREDICATE_SORTS, "shifting")
    evidence_sort = _coerce_sort(evidence_sort, VALID_EVIDENCE_SORTS, "observed_desc")

    summaries = dashboard_svc.predicate_summary(db)
    if category:
        summaries = [s for s in summaries if s.category == category]
    if only_no_recent:
        summaries = [s for s in summaries if not s.has_recent_evidence]
    summaries = dashboard_svc.sort_summaries(summaries, sort)

    categories = sorted({s.category for s in dashboard_svc.predicate_summary(db)})

    evidence_rows: list = []
    evidence_total = 0
    if tab == "evidence":
        evidence_rows, evidence_total = dashboard_svc.evidence_list(
            db, sort=evidence_sort, offset=evidence_offset,
        )

    # Stage-4: scenarios tab populated from the headless service.
    scenario_summaries: list = []
    if tab == "scenarios":
        scenario_summaries = dashboard_svc.scenario_summary(db)

    return templates.TemplateResponse(request, "scenarios_index.html", {
        "user": user,
        "tab": tab,
        "sort": sort,
        "category": category,
        "categories": categories,
        "only_no_recent": bool(only_no_recent),
        "summaries": summaries,
        "scenario_summaries": scenario_summaries,
        "evidence_sort": evidence_sort,
        "evidence_offset": evidence_offset,
        "evidence_rows": evidence_rows,
        "evidence_total": evidence_total,
        "evidence_page_size": dashboard_svc.EVIDENCE_LIST_PAGE_SIZE,
        "header": dashboard_svc.header_counts(db),
        "valid_predicate_sorts": VALID_PREDICATE_SORTS,
        "valid_evidence_sorts": VALID_EVIDENCE_SORTS,
        "review_digest": review_svc.latest_digest(db),
    })


@router.get("/scenarios/evidence", response_class=HTMLResponse)
def scenarios_evidence_alias(
    request: Request,
    sort: str = "observed_desc",
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Deep-link alias for the evidence tab. Calls into the main route
    so output is identical and the same template handles both."""
    return scenarios_index(
        request,
        tab="evidence",
        sort="shifting",
        category=None,
        only_no_recent=0,
        evidence_sort=sort,
        evidence_offset=offset,
        db=db,
        user=user,
    )


@router.get(
    "/scenarios/predicates/{predicate_key}/expand",
    response_class=HTMLResponse,
)
def predicate_expand(
    predicate_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """HTMX partial: the inline expand panel for one predicate card.
    Targets `#predicate-detail-{key}` on the parent page."""
    detail = dashboard_svc.predicate_detail(db, predicate_key)
    if detail is None:
        raise HTTPException(404, f"prediction {predicate_key!r} not found or inactive")
    pred = (
        db.query(Predicate).filter(Predicate.key == predicate_key).one_or_none()
    )
    review = review_svc.latest_review_for(db, predicate_key)
    proposals = review_svc.pending_proposals_for(db, predicate_key)
    return templates.TemplateResponse(request, "_scenarios_predicate_detail.html", {
        "detail": detail,
        "p": pred,
        "review": review,
        "pending_proposals": proposals,
        "proposals_by_id": {pr.id: pr for pr in proposals},
    })


@router.get(
    "/scenarios/scenarios/{scenario_key}/expand",
    response_class=HTMLResponse,
)
def scenario_expand(
    scenario_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """HTMX partial: the inline expand panel for one scenario card.
    Same shape as predicate_expand — targets `#scenario-detail-{key}`."""
    detail = dashboard_svc.scenario_detail(db, scenario_key)
    if detail is None:
        raise HTTPException(404, f"scenario {scenario_key!r} not found or inactive")
    return templates.TemplateResponse(request, "_scenarios_scenario_detail.html", {
        "detail": detail,
    })


# ── Stage-5: predicate / scenario edit affordances ─────────────────────
#
# GET routes return an inline form partial that swaps over the card via
# `outerHTML` on `#predicate-card-{key}` (or `#scenario-card-{key}`).
# Cancelling re-renders the card from server state. POST applies the
# edit through the service layer and re-renders the card on success or
# the form (with an error banner) on validation failure.

PREDICATE_CATEGORIES = ("discovery", "evaluation", "transaction", "control_point")


@router.get(
    "/scenarios/predicates/{predicate_key}/card",
    response_class=HTMLResponse,
)
def predicate_card_partial(
    predicate_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Render just one predicate card. Used by the edit form's Cancel
    handler to swap the form back out for the read-only card."""
    summary = _summary_for_predicate(db, predicate_key)
    if summary is None:
        return templates.TemplateResponse(
            request,
            "_scenarios_predicate_inactive_stub.html",
            {"key": predicate_key},
        )
    return templates.TemplateResponse(
        request,
        "_scenarios_predicate_card.html",
        {"s": summary},
    )


@router.get(
    "/scenarios/scenarios/{scenario_key}/card",
    response_class=HTMLResponse,
)
def scenario_card_partial(
    scenario_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Render just one scenario card — Cancel target for scenario edits."""
    summary = _summary_for_scenario(db, scenario_key)
    if summary is None:
        return templates.TemplateResponse(
            request,
            "_scenarios_scenario_inactive_stub.html",
            {"key": scenario_key},
        )
    return templates.TemplateResponse(
        request,
        "_scenarios_scenario_card.html",
        {"s": summary},
    )


def _summary_for_predicate(db: Session, predicate_key: str):
    """Find one predicate's PredicateSummary by linear scan over the
    full list. Cheap — N < 50 in practice — and reuses the same query
    path the grid already exercises so the card render is identical."""
    for s in dashboard_svc.predicate_summary(db):
        if s.key == predicate_key:
            return s
    return None


def _summary_for_scenario(db: Session, scenario_key: str):
    for s in dashboard_svc.scenario_summary(db):
        if s.key == scenario_key:
            return s
    return None


@router.get(
    "/scenarios/predicates/{predicate_key}/edit",
    response_class=HTMLResponse,
)
def predicate_edit_form(
    predicate_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst", "admin")),
):
    """Return the inline edit form pre-filled with the current predicate
    fields and state rows. Targets `#predicate-card-{key}` with
    `outerHTML` swap so the form replaces the card while editing."""
    pred = (
        db.query(Predicate).filter(Predicate.key == predicate_key).one_or_none()
    )
    if pred is None:
        raise HTTPException(404, f"prediction {predicate_key!r} not found")
    states = (
        db.query(PredicateState)
        .filter(PredicateState.predicate_id == pred.id)
        .order_by(PredicateState.ordinal_position)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "_scenarios_predicate_edit_form.html",
        {
            "p": pred,
            "states": states,
            "categories": PREDICATE_CATEGORIES,
            "error": None,
        },
    )


@router.post(
    "/scenarios/predicates/{predicate_key}/edit",
    response_class=HTMLResponse,
)
async def predicate_edit_apply(
    predicate_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst", "admin")),
):
    """Apply the form post. On success re-render the card; on validation
    failure re-render the form with the error message inline so the user
    keeps their in-progress edits."""
    form = await request.form()
    name = form.get("name") or None
    statement = form.get("statement") or None
    category = form.get("category") or None
    active = form.get("active") == "on"
    half_raw = (form.get("decay_half_life_days") or "").strip()
    if half_raw == "":
        half_life: int | None = None
    else:
        try:
            half_life = int(half_raw)
        except ValueError:
            return _predicate_edit_with_error(
                request, db, predicate_key,
                "decay_half_life_days must be an integer or blank.",
            )

    # State rows: keys like state_label_<state_key>, state_prior_<state_key>.
    states_payload: list[dict] = []
    for k, v in form.multi_items():
        if k.startswith("state_prior_"):
            sk = k[len("state_prior_"):]
            try:
                prior = float(v)
            except (TypeError, ValueError):
                return _predicate_edit_with_error(
                    request, db, predicate_key,
                    f"prior for state {sk!r} must be a number.",
                )
            label = form.get(f"state_label_{sk}") or None
            states_payload.append({
                "state_key": sk,
                "label": label,
                "prior_probability": prior,
            })

    try:
        svc_update_predicate(
            db, predicate_key,
            name=name, statement=statement, category=category, active=active,
            decay_half_life_days=half_life,
            decay_half_life_days_explicit=True,
            states=states_payload or None,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        return _predicate_edit_with_error(request, db, predicate_key, str(e))

    summary = _summary_for_predicate(db, predicate_key)
    if summary is None:
        # Predicate was just deactivated → the active-only summary list
        # no longer includes it. Render a minimal "card stub" so the
        # HTMX swap target is replaced with something visible rather
        # than disappearing silently.
        return templates.TemplateResponse(
            request,
            "_scenarios_predicate_inactive_stub.html",
            {"key": predicate_key},
        )
    return templates.TemplateResponse(
        request,
        "_scenarios_predicate_card.html",
        {"s": summary},
    )


def _predicate_edit_with_error(
    request: Request, db: Session, predicate_key: str, msg: str,
) -> HTMLResponse:
    pred = (
        db.query(Predicate).filter(Predicate.key == predicate_key).one_or_none()
    )
    if pred is None:
        raise HTTPException(404, f"prediction {predicate_key!r} not found")
    states = (
        db.query(PredicateState)
        .filter(PredicateState.predicate_id == pred.id)
        .order_by(PredicateState.ordinal_position)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "_scenarios_predicate_edit_form.html",
        {
            "p": pred,
            "states": states,
            "categories": PREDICATE_CATEGORIES,
            "error": msg,
        },
        status_code=400,
    )


@router.get(
    "/scenarios/scenarios/{scenario_key}/edit",
    response_class=HTMLResponse,
)
def scenario_edit_form(
    scenario_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst", "admin")),
):
    """Return the inline edit form for one scenario, pre-filled with its
    fields + per-link weight + required_state rows."""
    sc = db.query(Scenario).filter(Scenario.key == scenario_key).one_or_none()
    if sc is None:
        raise HTTPException(404, f"scenario {scenario_key!r} not found")
    return templates.TemplateResponse(
        request,
        "_scenarios_scenario_edit_form.html",
        _scenario_edit_context(db, sc, error=None),
    )


@router.post(
    "/scenarios/scenarios/{scenario_key}/edit",
    response_class=HTMLResponse,
)
async def scenario_edit_apply(
    scenario_key: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst", "admin")),
):
    form = await request.form()
    name = form.get("name") or None
    description = form.get("description")
    if description is None:
        description = None
    active = form.get("active") == "on"

    links_payload: list[dict] = []
    for k, v in form.multi_items():
        if k.startswith("link_weight_"):
            pkey = k[len("link_weight_"):]
            try:
                weight = float(v)
            except (TypeError, ValueError):
                return _scenario_edit_with_error(
                    request, db, scenario_key,
                    f"weight for link → {pkey!r} must be a number.",
                )
            req_state = form.get(f"link_state_{pkey}") or None
            links_payload.append({
                "predicate_key": pkey,
                "weight": weight,
                "required_state_key": req_state,
            })

    try:
        svc_update_scenario(
            db, scenario_key,
            name=name, description=description, active=active,
            links=links_payload or None,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        return _scenario_edit_with_error(request, db, scenario_key, str(e))

    summary = _summary_for_scenario(db, scenario_key)
    if summary is None:
        return templates.TemplateResponse(
            request,
            "_scenarios_scenario_inactive_stub.html",
            {"key": scenario_key},
        )
    return templates.TemplateResponse(
        request,
        "_scenarios_scenario_card.html",
        {"s": summary},
    )


def _scenario_edit_context(db: Session, sc: Scenario, *, error: str | None) -> dict:
    """Build the template ctx with each link enriched by the predicate
    name + the candidate states for the required_state_key dropdown."""
    pred_id_to_row = {p.id: p for p in db.query(Predicate).all()}
    states_by_pred: dict[int, list[PredicateState]] = {}
    for s in db.query(PredicateState).order_by(PredicateState.ordinal_position).all():
        states_by_pred.setdefault(s.predicate_id, []).append(s)

    links_view: list[dict] = []
    for ln in (
        db.query(ScenarioPredicateLink)
        .filter(ScenarioPredicateLink.scenario_id == sc.id)
        .all()
    ):
        pred = pred_id_to_row.get(ln.predicate_id)
        if pred is None:
            continue
        candidate_states = [
            {"state_key": s.state_key, "label": s.label}
            for s in states_by_pred.get(pred.id, [])
        ]
        links_view.append({
            "predicate_key": pred.key,
            "predicate_name": pred.name,
            "required_state_key": ln.required_state_key,
            "weight": ln.weight,
            "candidate_states": candidate_states,
        })
    # Stable sort by predicate key so re-renders don't reshuffle rows on
    # the user mid-edit.
    links_view.sort(key=lambda r: r["predicate_key"])
    return {"sc": sc, "links": links_view, "error": error}


def _scenario_edit_with_error(
    request: Request, db: Session, scenario_key: str, msg: str,
) -> HTMLResponse:
    sc = db.query(Scenario).filter(Scenario.key == scenario_key).one_or_none()
    if sc is None:
        raise HTTPException(404, f"scenario {scenario_key!r} not found")
    return templates.TemplateResponse(
        request,
        "_scenarios_scenario_edit_form.html",
        _scenario_edit_context(db, sc, error=msg),
        status_code=400,
    )
