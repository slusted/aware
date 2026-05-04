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

from datetime import datetime
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
    SignalView,
    User,
)
from ..scenarios.service import recompute_predicate


router = APIRouter(tags=["scenarios"], include_in_schema=False)
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)


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
    return templates.TemplateResponse(request, "_predicate_form.html", {
        "f": f,
        "evidence_rows": rows_view,
        "predicates": _predicates_with_states(db),
        "directions": VALID_DIRECTIONS,
        "strengths": VALID_STRENGTHS,
        "max_per_finding": MAX_EVIDENCE_PER_FINDING,
        "can_add_more": active_count < MAX_EVIDENCE_PER_FINDING,
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
        raise HTTPException(404, "predicate not found")
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
        raise HTTPException(400, f"unknown or inactive predicate {predicate_key!r}")
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
            f"unknown state {target_state_key!r} for predicate {predicate_key!r}",
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
