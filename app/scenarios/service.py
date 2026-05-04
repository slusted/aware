"""DB-aware orchestration for the Scenarios belief engine.

Loads predicates / evidence / config from the DB, hands them to
posterior.py, writes snapshots and updates cached `current_probability`
on PredicateState. No HTTP, no LLM — pure read-then-compute-then-write.

Stage-1 entrypoints:
  recompute_predicate(db, predicate_id, *, run_id=None) -> dict[state_key, prob]
  recompute_all(db, *, run_id=None) -> dict[predicate_key, dict[state_key, prob]]
  scenario_probabilities(db) -> dict[scenario_key, prob]

The job wrapper in app/jobs.py calls recompute_all inside a Run; the
scripts/add_evidence.py CLI calls recompute_predicate to fast-path a
single predicate after one new evidence row.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..models import (
    Predicate,
    PredicateState,
    PredicateEvidence,
    PredicatePosteriorSnapshot,
    Scenario,
    ScenarioPredicateLink,
    EvidenceLikelihoodRatio,
    SourceCredibilityDefault,
    ScenarioSetting,
)
from .posterior import (
    EvidenceInput,
    ScenarioInput,
    ScenarioLink,
    compute_posterior,
    compute_scenario_probabilities,
    compute_sensitivity,
)


# ─── Config readers ─────────────────────────────────────────────────────

def load_likelihood_table(db: Session) -> dict[tuple[str, str], float]:
    """Read evidence_likelihood_ratios into the lookup posterior.compute
    expects. Falls back to {} if the table is empty — the verify script
    catches that, but defense in depth keeps recompute from crashing."""
    return {
        (lr.direction, lr.strength_bucket): lr.multiplier
        for lr in db.query(EvidenceLikelihoodRatio).all()
    }


def load_source_credibility(db: Session) -> dict[str, float]:
    """source_type → credibility. Used by the CLI when inserting evidence
    to default the per-row credibility from the finding's source."""
    return {
        row.source_type: row.credibility
        for row in db.query(SourceCredibilityDefault).all()
    }


def load_setting(db: Session, key: str, default):
    """Reads a ScenarioSetting JSON value's `value` field, or returns
    `default` if the row or shape is missing."""
    row = db.query(ScenarioSetting).filter(ScenarioSetting.key == key).first()
    if row is None or not isinstance(row.value, dict):
        return default
    return row.value.get("value", default)


def default_decay_half_life(db: Session) -> float:
    """Global decay half-life in days. Per-predicate override on
    Predicate.decay_half_life_days takes precedence at compute time."""
    return float(load_setting(db, "default_decay_half_life_days", 60))


# ─── Predicate posterior recompute ──────────────────────────────────────

def _evidence_for_predicate(db: Session, predicate_id: int) -> list[EvidenceInput]:
    """Confirmed evidence rows for one predicate, mapped to the math layer's
    NamedTuple shape. Order doesn't matter — the update is commutative."""
    rows = (
        db.query(PredicateEvidence)
        .filter(
            PredicateEvidence.predicate_id == predicate_id,
            PredicateEvidence.confirmed_at.isnot(None),
        )
        .all()
    )
    return [
        EvidenceInput(
            target_state_key=r.target_state_key,
            direction=r.direction,
            strength_bucket=r.strength_bucket,
            credibility=r.credibility,
            observed_at=r.observed_at,
        )
        for r in rows
    ]


def recompute_predicate(
    db: Session,
    predicate_id: int,
    *,
    run_id: int | None = None,
    now: datetime | None = None,
    likelihood_table: dict[tuple[str, str], float] | None = None,
    global_half_life: float | None = None,
) -> dict[str, float]:
    """Recompute one predicate's posterior, write a snapshot row per
    state, and update the cached `current_probability` on PredicateState.

    Caller owns the transaction boundary. We add rows + flush, but don't
    commit — recompute_all wraps multiple predicates in one commit so a
    partial failure doesn't leave half-recomputed state behind.

    Returns the posterior dict for convenience (CLI uses it to print
    before/after).
    """
    pred = db.get(Predicate, predicate_id)
    if pred is None:
        return {}

    states = (
        db.query(PredicateState)
        .filter(PredicateState.predicate_id == predicate_id)
        .all()
    )
    if not states:
        return {}

    if likelihood_table is None:
        likelihood_table = load_likelihood_table(db)
    if global_half_life is None:
        global_half_life = default_decay_half_life(db)
    if now is None:
        now = datetime.utcnow()

    half_life = pred.decay_half_life_days or global_half_life
    prior = {s.state_key: s.prior_probability for s in states}
    evidence = _evidence_for_predicate(db, predicate_id)

    posterior = compute_posterior(
        prior=prior,
        evidence=evidence,
        likelihood_table=likelihood_table,
        half_life_days=half_life,
        now=now,
    )

    # Update cache + write snapshots in one pass.
    by_key = {s.state_key: s for s in states}
    for state_key, p in posterior.items():
        s = by_key.get(state_key)
        if s is None:
            continue
        s.current_probability = p
        s.updated_at = now
        db.add(PredicatePosteriorSnapshot(
            predicate_id=predicate_id,
            state_key=state_key,
            probability=p,
            evidence_count=len(evidence),
            run_id=run_id,
            computed_at=now,
        ))
    db.flush()
    return posterior


def recompute_all(
    db: Session,
    *,
    run_id: int | None = None,
    predicate_keys: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, float]]:
    """Recompute every active predicate (or the named subset). Loads the
    likelihood table and global half-life once, threads them into per-
    predicate calls. Returns {predicate_key: {state_key: probability}}.

    Caller commits.
    """
    likelihood_table = load_likelihood_table(db)
    global_half_life = default_decay_half_life(db)
    if now is None:
        now = datetime.utcnow()

    q = db.query(Predicate).filter(Predicate.active.is_(True))
    if predicate_keys:
        q = q.filter(Predicate.key.in_(predicate_keys))
    predicates = q.all()

    out: dict[str, dict[str, float]] = {}
    for p in predicates:
        out[p.key] = recompute_predicate(
            db,
            p.id,
            run_id=run_id,
            now=now,
            likelihood_table=likelihood_table,
            global_half_life=global_half_life,
        )
    return out


# ─── Scenario derivation ────────────────────────────────────────────────

def _load_scenario_inputs(db: Session) -> list[ScenarioInput]:
    """Read scenarios + links, joining predicate keys onto links so the
    posterior layer can match by key (its only identifier)."""
    pred_id_to_key = {p.id: p.key for p in db.query(Predicate).all()}
    links_by_scenario: dict[int, list[ScenarioLink]] = {}
    for ln in db.query(ScenarioPredicateLink).all():
        pkey = pred_id_to_key.get(ln.predicate_id)
        if pkey is None:
            continue
        links_by_scenario.setdefault(ln.scenario_id, []).append(
            ScenarioLink(
                predicate_key=pkey,
                required_state_key=ln.required_state_key,
                weight=ln.weight,
            )
        )
    out: list[ScenarioInput] = []
    for sc in db.query(Scenario).filter(Scenario.active.is_(True)).all():
        out.append(ScenarioInput(
            key=sc.key,
            links=links_by_scenario.get(sc.id, []),
        ))
    return out


def _current_predicate_posteriors(db: Session) -> dict[str, dict[str, float]]:
    """Read cached current_probability across all active predicates as the
    {predicate_key: {state_key: probability}} shape posterior.py expects."""
    out: dict[str, dict[str, float]] = {}
    pred_id_to_key = {
        p.id: p.key
        for p in db.query(Predicate).filter(Predicate.active.is_(True)).all()
    }
    for s in db.query(PredicateState).all():
        pkey = pred_id_to_key.get(s.predicate_id)
        if pkey is None:
            continue
        out.setdefault(pkey, {})[s.state_key] = s.current_probability
    return out


def scenario_probabilities(db: Session) -> dict[str, float]:
    """Live P(scenario) over current predicate posteriors. Cheap — pure
    math over already-cached probabilities. Safe to call from any
    request handler. Stage 4 dashboard reads from this."""
    posteriors = _current_predicate_posteriors(db)
    scenarios = _load_scenario_inputs(db)
    return compute_scenario_probabilities(posteriors, scenarios)


def scenario_sensitivity(
    db: Session,
    predicate_key: str,
    target_state_key: str,
    delta: float = 0.05,
) -> dict[str, float]:
    """∂P(scenario)/∂P(predicate=state). See posterior.compute_sensitivity."""
    posteriors = _current_predicate_posteriors(db)
    scenarios = _load_scenario_inputs(db)
    return compute_sensitivity(
        posteriors, scenarios, predicate_key, target_state_key, delta
    )
