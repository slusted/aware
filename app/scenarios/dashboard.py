"""Read-only service layer for the predicate dashboard.

Builds the dict shapes the Stage-3 templates render. No mutations; all
values derived from the existing tables (`predicates`, `predicate_states`,
`predicate_evidence`, `predicate_posterior_snapshots`, `findings`).

Functions:
  predicate_summary  → list, one entry per active predicate (powers grid)
  predicate_detail   → per-predicate expansion (sparklines + evidence)
  evidence_list      → flat confirmed-evidence list (Evidence tab)

See docs/scenarios/03-predicate-dashboard.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy.orm import Session

from ..models import (
    Predicate,
    PredicateState,
    PredicateEvidence,
    PredicatePosteriorSnapshot,
    Finding,
)
from .posterior import (
    decay_factor,
    downsample_series,
    log_odds_contribution,
    shannon_entropy,
    velocity_pp,
)
from .service import default_decay_half_life, load_likelihood_table


SPARKLINE_WINDOW_DAYS = 90
SPARKLINE_MAX_POINTS = 100
EVIDENCE_RECENT_WINDOW_DAYS = 30


# ─── Shapes ─────────────────────────────────────────────────────────────

class StateView(NamedTuple):
    state_key: str
    label: str
    ordinal_position: int
    prior: float
    current: float
    velocity_pp_30d: float


class PredicateSummary(NamedTuple):
    key: str
    name: str
    statement: str
    category: str
    states: list[StateView]
    entropy: float                              # 0–1, "most contested" sort
    dominant_state_key: str                     # highest current_probability
    dominant_state_label: str
    dominant_state_velocity_pp_30d: float       # signed, drives the velocity pill
    evidence_count: int                         # confirmed only
    last_updated_at: datetime | None            # newest snapshot or evidence touch
    has_recent_evidence: bool                   # confirmed in last 30 days
    sparkline_dominant: list[tuple[datetime, float]]  # (timestamp, prob) for dominant state


class EvidenceContribView(NamedTuple):
    """One evidence row as the detail / evidence-tab template wants it.
    `contribution` is the live log-odds delta this evidence adds today
    (decayed). Sorts the detail's evidence table descending by |c|."""
    id: int
    finding_id: int | None
    finding_title: str | None
    finding_url: str | None
    competitor: str | None
    target_state_key: str
    target_state_label: str
    direction: str
    strength_bucket: str
    credibility: float
    classified_by: str
    observed_at: datetime
    confirmed_at: datetime | None
    notes: str | None
    contribution: float


class PredicateDetail(NamedTuple):
    summary: PredicateSummary
    sparklines: dict[str, list[tuple[datetime, float]]]   # state_key → series
    evidence_confirmed: list[EvidenceContribView]
    evidence_pending: list[EvidenceContribView]           # llm proposals not yet confirmed
    evidence_rejected: list[EvidenceContribView]


# ─── Helpers ────────────────────────────────────────────────────────────

def _state_label_lookup(db: Session) -> dict[tuple[int, str], tuple[str, int, float]]:
    """{(predicate_id, state_key): (label, ordinal_position, prior)}.
    One query, used wherever evidence rows need to render a human label."""
    out: dict[tuple[int, str], tuple[str, int, float]] = {}
    for s in db.query(PredicateState).all():
        out[(s.predicate_id, s.state_key)] = (s.label, s.ordinal_position, s.prior_probability)
    return out


def _snapshot_series_by_state(
    db: Session,
    predicate_id: int,
    *,
    since: datetime,
) -> dict[str, list[tuple[datetime, float]]]:
    """Per-state snapshot series for one predicate, since the cutoff,
    oldest first. One query, grouped in Python."""
    rows = (
        db.query(PredicatePosteriorSnapshot)
        .filter(
            PredicatePosteriorSnapshot.predicate_id == predicate_id,
            PredicatePosteriorSnapshot.computed_at >= since,
        )
        .order_by(PredicatePosteriorSnapshot.computed_at.asc())
        .all()
    )
    out: dict[str, list[tuple[datetime, float]]] = {}
    for s in rows:
        out.setdefault(s.state_key, []).append((s.computed_at, s.probability))
    return out


def _baseline_for_state(
    snapshots_for_state: list[tuple[datetime, float]],
    *,
    target: datetime,
    fallback: float,
) -> float:
    """Return the snapshot probability closest to (and at-or-before)
    `target`, falling back to `fallback` (typically the prior) if no
    snapshot is older than the target. Used to compute 30-day velocity."""
    if not snapshots_for_state:
        return fallback
    older = [(t, p) for t, p in snapshots_for_state if t <= target]
    if not older:
        # No snapshot that old — use the oldest available as the
        # next-best baseline. Prevents a "30d velocity" of NaN on a
        # fresh install.
        return snapshots_for_state[0][1]
    return older[-1][1]


# ─── predicate_summary ─────────────────────────────────────────────────

def predicate_summary(db: Session, *, now: datetime | None = None) -> list[PredicateSummary]:
    """One PredicateSummary per active predicate. Cheap-ish: one query
    each for predicates, states, snapshots, evidence counts."""
    if now is None:
        now = datetime.utcnow()
    cutoff_30d = now - timedelta(days=EVIDENCE_RECENT_WINDOW_DAYS)
    cutoff_sparkline = now - timedelta(days=SPARKLINE_WINDOW_DAYS)

    preds = (
        db.query(Predicate)
        .filter(Predicate.active.is_(True))
        .order_by(Predicate.id)
        .all()
    )
    if not preds:
        return []

    # Bulk loads — one query each rather than per-predicate.
    states_by_pred: dict[int, list[PredicateState]] = {}
    for s in db.query(PredicateState).all():
        states_by_pred.setdefault(s.predicate_id, []).append(s)

    # Confirmed evidence counts per predicate.
    from sqlalchemy import func as _func
    ev_counts: dict[int, int] = {}
    for predicate_id, n in (
        db.query(PredicateEvidence.predicate_id, _func.count(PredicateEvidence.id))
        .filter(PredicateEvidence.confirmed_at.isnot(None))
        .filter(PredicateEvidence.classified_by != "user_rejected")
        .group_by(PredicateEvidence.predicate_id)
        .all()
    ):
        ev_counts[predicate_id] = int(n)

    # Recent confirmed evidence flag.
    recent_pred_ids: set[int] = {
        pid for (pid,) in
        db.query(PredicateEvidence.predicate_id)
        .filter(
            PredicateEvidence.confirmed_at.isnot(None),
            PredicateEvidence.confirmed_at >= cutoff_30d,
            PredicateEvidence.classified_by != "user_rejected",
        )
        .distinct()
        .all()
    }

    out: list[PredicateSummary] = []
    for p in preds:
        state_rows = sorted(
            states_by_pred.get(p.id, []),
            key=lambda s: s.ordinal_position,
        )
        if not state_rows:
            continue
        snaps_by_state = _snapshot_series_by_state(db, p.id, since=cutoff_sparkline)

        # Per-state velocity (30d) using snapshot baselines.
        baseline_target = now - timedelta(days=EVIDENCE_RECENT_WINDOW_DAYS)
        states_view: list[StateView] = []
        probs_dict: dict[str, float] = {}
        for s in state_rows:
            baseline = _baseline_for_state(
                snaps_by_state.get(s.state_key, []),
                target=baseline_target,
                fallback=s.prior_probability,
            )
            v = velocity_pp(baseline, s.current_probability)
            states_view.append(StateView(
                state_key=s.state_key,
                label=s.label,
                ordinal_position=s.ordinal_position,
                prior=s.prior_probability,
                current=s.current_probability,
                velocity_pp_30d=v,
            ))
            probs_dict[s.state_key] = s.current_probability

        dominant = max(states_view, key=lambda sv: sv.current)

        # Last-updated = newest snapshot for this predicate, or fall
        # back to the newest evidence touch.
        last_snap_at = (
            db.query(_func.max(PredicatePosteriorSnapshot.computed_at))
            .filter(PredicatePosteriorSnapshot.predicate_id == p.id)
            .scalar()
        )

        sparkline_dominant = downsample_series(
            snaps_by_state.get(dominant.state_key, []),
            max_points=SPARKLINE_MAX_POINTS,
        )

        out.append(PredicateSummary(
            key=p.key,
            name=p.name,
            statement=p.statement,
            category=p.category,
            states=states_view,
            entropy=shannon_entropy(probs_dict),
            dominant_state_key=dominant.state_key,
            dominant_state_label=dominant.label,
            dominant_state_velocity_pp_30d=dominant.velocity_pp_30d,
            evidence_count=ev_counts.get(p.id, 0),
            last_updated_at=last_snap_at,
            has_recent_evidence=p.id in recent_pred_ids,
            sparkline_dominant=sparkline_dominant,
        ))
    return out


def sort_summaries(
    summaries: list[PredicateSummary],
    sort_key: str,
) -> list[PredicateSummary]:
    """Stable sort by the dropdown's key. Unknown keys fall back to
    'shifting'."""
    if sort_key == "contested":
        return sorted(summaries, key=lambda s: s.entropy, reverse=True)
    if sort_key == "alpha":
        return sorted(summaries, key=lambda s: s.key)
    if sort_key == "category":
        return sorted(summaries, key=lambda s: (s.category, s.key))
    # default: most shifting
    return sorted(
        summaries,
        key=lambda s: abs(s.dominant_state_velocity_pp_30d),
        reverse=True,
    )


# ─── predicate_detail ──────────────────────────────────────────────────

def _evidence_to_contrib_view(
    ev: PredicateEvidence,
    state_label_lookup: dict[tuple[int, str], tuple[str, int, float]],
    finding_by_id: dict[int, Finding],
    likelihood_table: dict[tuple[str, str], float],
    half_life_days: float,
    now: datetime,
) -> EvidenceContribView:
    label_info = state_label_lookup.get(
        (ev.predicate_id, ev.target_state_key), (ev.target_state_key, 0, 0.0),
    )
    finding = finding_by_id.get(ev.finding_id) if ev.finding_id else None
    contrib = log_odds_contribution(
        direction=ev.direction,
        strength_bucket=ev.strength_bucket,
        credibility=ev.credibility,
        observed_at=ev.observed_at,
        likelihood_table=likelihood_table,
        half_life_days=half_life_days,
        now=now,
    )
    return EvidenceContribView(
        id=ev.id,
        finding_id=ev.finding_id,
        finding_title=finding.title if finding else None,
        finding_url=finding.url if finding else None,
        competitor=finding.competitor if finding else None,
        target_state_key=ev.target_state_key,
        target_state_label=label_info[0],
        direction=ev.direction,
        strength_bucket=ev.strength_bucket,
        credibility=ev.credibility,
        classified_by=ev.classified_by,
        observed_at=ev.observed_at,
        confirmed_at=ev.confirmed_at,
        notes=ev.notes,
        contribution=contrib,
    )


def predicate_detail(
    db: Session,
    predicate_key: str,
    *,
    now: datetime | None = None,
) -> PredicateDetail | None:
    """Per-predicate expansion: sparklines for every state + evidence
    drill-down with live log-odds contributions. Returns None when the
    predicate doesn't exist or is inactive (route can 404)."""
    if now is None:
        now = datetime.utcnow()
    pred = (
        db.query(Predicate)
        .filter(Predicate.key == predicate_key, Predicate.active.is_(True))
        .one_or_none()
    )
    if pred is None:
        return None

    # Reuse predicate_summary but pull only the matching entry — saves
    # writing the math twice. Cheap enough at our scale.
    all_summaries = predicate_summary(db, now=now)
    summary = next((s for s in all_summaries if s.key == predicate_key), None)
    if summary is None:
        return None

    cutoff = now - timedelta(days=SPARKLINE_WINDOW_DAYS)
    snaps_by_state = _snapshot_series_by_state(db, pred.id, since=cutoff)
    sparklines = {
        k: downsample_series(v, max_points=SPARKLINE_MAX_POINTS)
        for k, v in snaps_by_state.items()
    }
    # Make sure every state has an entry, even if no snapshots — keeps
    # the legend stable in the UI.
    for sv in summary.states:
        sparklines.setdefault(sv.state_key, [])

    # Evidence load (single query, then split by status). Order: oldest
    # first; the contribution sort below re-orders for display.
    ev_rows = (
        db.query(PredicateEvidence)
        .filter(PredicateEvidence.predicate_id == pred.id)
        .order_by(PredicateEvidence.observed_at.asc())
        .all()
    )
    finding_ids = [ev.finding_id for ev in ev_rows if ev.finding_id]
    finding_by_id: dict[int, Finding] = {}
    if finding_ids:
        for f in db.query(Finding).filter(Finding.id.in_(finding_ids)).all():
            finding_by_id[f.id] = f

    state_label_lookup = _state_label_lookup(db)
    likelihood_table = load_likelihood_table(db)
    half_life = pred.decay_half_life_days or default_decay_half_life(db)

    confirmed: list[EvidenceContribView] = []
    pending: list[EvidenceContribView] = []
    rejected: list[EvidenceContribView] = []
    for ev in ev_rows:
        view = _evidence_to_contrib_view(
            ev, state_label_lookup, finding_by_id,
            likelihood_table, half_life, now,
        )
        if ev.classified_by == "user_rejected":
            rejected.append(view)
        elif ev.confirmed_at is None:
            pending.append(view)
        else:
            confirmed.append(view)

    confirmed.sort(key=lambda v: abs(v.contribution), reverse=True)
    pending.sort(key=lambda v: v.observed_at, reverse=True)
    rejected.sort(key=lambda v: v.observed_at, reverse=True)

    return PredicateDetail(
        summary=summary,
        sparklines=sparklines,
        evidence_confirmed=confirmed,
        evidence_pending=pending,
        evidence_rejected=rejected,
    )


# ─── evidence_list ─────────────────────────────────────────────────────

EVIDENCE_LIST_PAGE_SIZE = 200


def evidence_list(
    db: Session,
    *,
    sort: str = "observed_desc",
    offset: int = 0,
    limit: int = EVIDENCE_LIST_PAGE_SIZE,
    now: datetime | None = None,
) -> tuple[list[EvidenceContribView], int]:
    """Flat confirmed evidence list. Returns (rows, total_count) so the
    template can render pagination state."""
    if now is None:
        now = datetime.utcnow()

    base_q = (
        db.query(PredicateEvidence)
        .filter(PredicateEvidence.confirmed_at.isnot(None))
        .filter(PredicateEvidence.classified_by != "user_rejected")
    )
    total = base_q.count()

    if sort == "observed_asc":
        ordered = base_q.order_by(PredicateEvidence.observed_at.asc())
    elif sort == "observed_desc":
        ordered = base_q.order_by(PredicateEvidence.observed_at.desc())
    elif sort == "confirmed_desc":
        ordered = base_q.order_by(PredicateEvidence.confirmed_at.desc())
    elif sort == "credibility_desc":
        ordered = base_q.order_by(PredicateEvidence.credibility.desc())
    else:
        ordered = base_q.order_by(PredicateEvidence.observed_at.desc())

    ev_rows = ordered.offset(offset).limit(limit).all()
    if not ev_rows:
        return [], total

    finding_ids = [ev.finding_id for ev in ev_rows if ev.finding_id]
    finding_by_id: dict[int, Finding] = {}
    if finding_ids:
        for f in db.query(Finding).filter(Finding.id.in_(finding_ids)).all():
            finding_by_id[f.id] = f

    state_label_lookup = _state_label_lookup(db)
    likelihood_table = load_likelihood_table(db)
    # Per-row half-life respects per-predicate overrides.
    half_life_by_pred: dict[int, float] = {}
    global_hl = default_decay_half_life(db)
    for p in db.query(Predicate).all():
        half_life_by_pred[p.id] = float(p.decay_half_life_days or global_hl)

    out: list[EvidenceContribView] = []
    for ev in ev_rows:
        out.append(_evidence_to_contrib_view(
            ev, state_label_lookup, finding_by_id,
            likelihood_table,
            half_life_by_pred.get(ev.predicate_id, global_hl),
            now,
        ))
    if sort == "contribution_desc":
        out.sort(key=lambda v: abs(v.contribution), reverse=True)
    return out, total


# ─── Page-level header counts ──────────────────────────────────────────

def header_counts(db: Session) -> dict:
    """Small dict of summary counts the dashboard header strip shows."""
    from sqlalchemy import func as _func
    n_predicates = (
        db.query(_func.count(Predicate.id))
        .filter(Predicate.active.is_(True))
        .scalar()
    ) or 0
    n_evidence_confirmed = (
        db.query(_func.count(PredicateEvidence.id))
        .filter(PredicateEvidence.confirmed_at.isnot(None))
        .filter(PredicateEvidence.classified_by != "user_rejected")
        .scalar()
    ) or 0
    last_recompute_at = (
        db.query(_func.max(PredicatePosteriorSnapshot.computed_at)).scalar()
    )
    return {
        "n_predicates": int(n_predicates),
        "n_evidence_confirmed": int(n_evidence_confirmed),
        "last_recompute_at": last_recompute_at,
    }
