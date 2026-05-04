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

import math
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy.orm import Session

from ..models import (
    Predicate,
    PredicateState,
    PredicateEvidence,
    PredicatePosteriorSnapshot,
    Scenario,
    ScenarioPredicateLink,
    Finding,
)
from .posterior import (
    decay_factor,
    downsample_series,
    log_odds_contribution,
    shannon_entropy,
    velocity_pp,
)
from .service import (
    default_decay_half_life,
    load_likelihood_table,
    scenario_probabilities,
    scenario_sensitivity,
)


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


# ─── Stage 4: scenario summary + detail ────────────────────────────────

class ScenarioSummary(NamedTuple):
    key: str
    name: str
    description: str
    probability: float                  # 0–1, current
    rank: int                           # 1 = highest probability
    constraint_count: int               # number of predicate links
    constraint_satisfaction: float      # weighted avg of P(predicate=required)
    weakest_link_predicate_key: str
    weakest_link_required_state: str
    weakest_link_required_state_label: str
    weakest_link_current_p: float


class ScenarioContribution(NamedTuple):
    """One row in the contributions breakdown — makes the
    `unnormalized = exp(Σ weight × log(P))` formula visible per-link."""
    predicate_key: str
    predicate_name: str
    required_state_key: str
    required_state_label: str
    weight: float
    current_p_required: float
    contribution: float                 # weight * log(P) — signed
    rank_within_scenario: int           # 1 = biggest |contribution|


class SensitivityRow(NamedTuple):
    predicate_key: str
    predicate_name: str
    target_state_key: str
    target_state_label: str
    delta_per_pp: float                 # ∂P(scenario) per 1pp move on this state


class ScenarioDetail(NamedTuple):
    summary: ScenarioSummary
    contributions: list[ScenarioContribution]
    sensitivity_to_predicates: list[SensitivityRow]


def _load_scenario_links_grouped(
    db: Session,
) -> tuple[dict[int, list[ScenarioPredicateLink]], dict[int, str], dict[int, str]]:
    """Returns (links_by_scenario_id, predicate_id_to_key, predicate_id_to_name).
    One pass per query; reused by both summary and detail."""
    links_by_scenario: dict[int, list[ScenarioPredicateLink]] = {}
    for ln in db.query(ScenarioPredicateLink).all():
        links_by_scenario.setdefault(ln.scenario_id, []).append(ln)
    pred_id_to_key: dict[int, str] = {}
    pred_id_to_name: dict[int, str] = {}
    for p in db.query(Predicate).all():
        pred_id_to_key[p.id] = p.key
        pred_id_to_name[p.id] = p.name
    return links_by_scenario, pred_id_to_key, pred_id_to_name


def _current_probabilities_by_predicate(
    db: Session,
) -> dict[int, dict[str, float]]:
    """{predicate_id: {state_key: current_probability}}. One query."""
    out: dict[int, dict[str, float]] = {}
    for s in db.query(PredicateState).all():
        out.setdefault(s.predicate_id, {})[s.state_key] = s.current_probability
    return out


def _state_label_lookup_by_id(db: Session) -> dict[tuple[int, str], str]:
    """{(predicate_id, state_key): label}. Reused per-row label resolution."""
    out: dict[tuple[int, str], str] = {}
    for s in db.query(PredicateState).all():
        out[(s.predicate_id, s.state_key)] = s.label
    return out


def scenario_summary(db: Session) -> list[ScenarioSummary]:
    """One ScenarioSummary per active scenario, sorted by probability desc.
    Cheap: pulls scenarios + links + predicate states in a few queries.
    Returns [] when no active scenarios are seeded."""
    scenarios = (
        db.query(Scenario)
        .filter(Scenario.active.is_(True))
        .order_by(Scenario.id)
        .all()
    )
    if not scenarios:
        return []

    probs = scenario_probabilities(db)
    links_by_scenario, pred_id_to_key, pred_id_to_name = _load_scenario_links_grouped(db)
    probs_by_pred = _current_probabilities_by_predicate(db)
    label_lookup = _state_label_lookup_by_id(db)

    summaries: list[ScenarioSummary] = []
    for sc in scenarios:
        links = links_by_scenario.get(sc.id, [])
        prob = probs.get(sc.key, 0.0)

        # Per-link satisfaction (weighted average of P(req_state)).
        weighted_sum = 0.0
        total_weight = 0.0
        weakest_link = None
        weakest_p = 1.0
        for ln in links:
            p_required = probs_by_pred.get(ln.predicate_id, {}).get(
                ln.required_state_key, 0.0,
            )
            weighted_sum += ln.weight * p_required
            total_weight += ln.weight
            if p_required < weakest_p:
                weakest_p = p_required
                weakest_link = ln
        constraint_satisfaction = (
            weighted_sum / total_weight if total_weight > 0 else 0.0
        )

        if weakest_link is not None:
            weakest_pred_key = pred_id_to_key.get(weakest_link.predicate_id, "?")
            weakest_state_label = label_lookup.get(
                (weakest_link.predicate_id, weakest_link.required_state_key),
                weakest_link.required_state_key,
            )
            weakest_required_state = weakest_link.required_state_key
        else:
            weakest_pred_key = ""
            weakest_state_label = ""
            weakest_required_state = ""

        summaries.append(ScenarioSummary(
            key=sc.key,
            name=sc.name,
            description=sc.description or "",
            probability=prob,
            rank=0,  # filled in after sort
            constraint_count=len(links),
            constraint_satisfaction=constraint_satisfaction,
            weakest_link_predicate_key=weakest_pred_key,
            weakest_link_required_state=weakest_required_state,
            weakest_link_required_state_label=weakest_state_label,
            weakest_link_current_p=weakest_p if weakest_link else 0.0,
        ))

    # Rank by probability desc.
    summaries.sort(key=lambda s: s.probability, reverse=True)
    return [s._replace(rank=i + 1) for i, s in enumerate(summaries)]


def scenario_detail(
    db: Session,
    scenario_key: str,
) -> ScenarioDetail | None:
    """Per-scenario expansion: contribution table + sensitivity. Returns
    None when the scenario doesn't exist or is inactive."""
    sc = (
        db.query(Scenario)
        .filter(Scenario.key == scenario_key, Scenario.active.is_(True))
        .one_or_none()
    )
    if sc is None:
        return None

    summaries = scenario_summary(db)
    summary = next((s for s in summaries if s.key == scenario_key), None)
    if summary is None:
        return None

    links_by_scenario, pred_id_to_key, pred_id_to_name = _load_scenario_links_grouped(db)
    probs_by_pred = _current_probabilities_by_predicate(db)
    label_lookup = _state_label_lookup_by_id(db)

    contributions: list[ScenarioContribution] = []
    for ln in links_by_scenario.get(sc.id, []):
        p_required = probs_by_pred.get(ln.predicate_id, {}).get(
            ln.required_state_key, 0.0,
        )
        # weight * log(P); guard log(0) by clamping at a tiny floor.
        if p_required <= 0:
            contrib = float("-inf")
        else:
            contrib = ln.weight * math.log(p_required)
        contributions.append(ScenarioContribution(
            predicate_key=pred_id_to_key.get(ln.predicate_id, "?"),
            predicate_name=pred_id_to_name.get(ln.predicate_id, "?"),
            required_state_key=ln.required_state_key,
            required_state_label=label_lookup.get(
                (ln.predicate_id, ln.required_state_key), ln.required_state_key,
            ),
            weight=ln.weight,
            current_p_required=p_required,
            contribution=contrib,
            rank_within_scenario=0,  # filled after sort
        ))
    contributions.sort(
        key=lambda c: abs(c.contribution) if c.contribution != float("-inf") else 1e9,
        reverse=True,
    )
    contributions = [c._replace(rank_within_scenario=i + 1) for i, c in enumerate(contributions)]

    # Sensitivity: walk every (predicate, state) the scenario constrains
    # and ask compute_sensitivity(...). The result is per-Δ where we
    # bumped 5pp; normalize to per-1pp so the table reads cleanly.
    BUMP_PP = 0.05
    sensitivity_rows: list[SensitivityRow] = []
    for ln in links_by_scenario.get(sc.id, []):
        sens = scenario_sensitivity(
            db,
            predicate_key=pred_id_to_key.get(ln.predicate_id, ""),
            target_state_key=ln.required_state_key,
            delta=BUMP_PP,
        )
        delta_per_pp = sens.get(scenario_key, 0.0) / 100.0  # per-pp not per-fraction
        sensitivity_rows.append(SensitivityRow(
            predicate_key=pred_id_to_key.get(ln.predicate_id, "?"),
            predicate_name=pred_id_to_name.get(ln.predicate_id, "?"),
            target_state_key=ln.required_state_key,
            target_state_label=label_lookup.get(
                (ln.predicate_id, ln.required_state_key), ln.required_state_key,
            ),
            delta_per_pp=delta_per_pp,
        ))
    sensitivity_rows.sort(key=lambda r: abs(r.delta_per_pp), reverse=True)

    return ScenarioDetail(
        summary=summary,
        contributions=contributions,
        sensitivity_to_predicates=sensitivity_rows,
    )


def evidence_for_finding(
    db: Session,
    finding_id: int,
    *,
    now: datetime | None = None,
) -> list[EvidenceContribView]:
    """All evidence rows attached to one finding, with live log-odds
    contributions. Used by the chat tool that answers "what predicates
    does finding #X bear on?". Includes pending + rejected; caller can
    filter by `classified_by` and `confirmed_at` if needed."""
    if now is None:
        now = datetime.utcnow()
    rows = (
        db.query(PredicateEvidence)
        .filter(PredicateEvidence.finding_id == finding_id)
        .order_by(PredicateEvidence.id.asc())
        .all()
    )
    if not rows:
        return []

    finding_by_id: dict[int, Finding] = {}
    f = db.get(Finding, finding_id)
    if f is not None:
        finding_by_id[finding_id] = f

    state_label_lookup = _state_label_lookup(db)
    likelihood_table = load_likelihood_table(db)
    half_life_by_pred: dict[int, float] = {}
    global_hl = default_decay_half_life(db)
    for p in db.query(Predicate).all():
        half_life_by_pred[p.id] = float(p.decay_half_life_days or global_hl)

    out: list[EvidenceContribView] = []
    for ev in rows:
        out.append(_evidence_to_contrib_view(
            ev, state_label_lookup, finding_by_id,
            likelihood_table,
            half_life_by_pred.get(ev.predicate_id, global_hl),
            now,
        ))
    return out


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
