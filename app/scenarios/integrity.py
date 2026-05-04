"""Sanity checks for the seeded predicate / scenario / config rows.

Schema constraints can't enforce all the invariants (priors summing to 1,
weights summing to 1, scenario-link state references). This module fills
that gap. Called by scripts/seed_scenarios.py after upsert; non-zero exit
on failure.

Returns a list of human-readable error strings. Empty list = clean.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import (
    Predicate,
    PredicateState,
    Scenario,
    ScenarioPredicateLink,
    EvidenceLikelihoodRatio,
    SourceCredibilityDefault,
    ScenarioSetting,
)


# Loose tolerance for float arithmetic. Priors/weights authored as
# 0.55 + 0.45 can land at 1.0000000000001 in floats — we don't want
# that to fail the integrity check.
SUM_TOL = 1e-6


def validate_seed(db: Session) -> list[str]:
    """Run every invariant and return any violations as plain strings.

    Invariants:
      1. Every active predicate has at least 2 states.
      2. Per-predicate prior_probability sums to 1.0 ± SUM_TOL.
      3. State keys are unique within a predicate (also enforced by UQ).
      4. Every scenario_predicate_link.required_state_key references a
         real PredicateState for that predicate.
      5. Per-scenario weights sum to 1.0 ± SUM_TOL.
      6. Scenarios reference at least 1 link.
      7. Likelihood table covers (support|contradict|neutral) ×
         (weak|moderate|strong) — all 9 combos present, multiplier > 0.
      8. Required scenario_settings keys exist.
    """
    errors: list[str] = []

    predicates = db.query(Predicate).all()
    states_by_pred: dict[int, list[PredicateState]] = {}
    for st in db.query(PredicateState).all():
        states_by_pred.setdefault(st.predicate_id, []).append(st)

    for p in predicates:
        if not p.active:
            continue
        states = states_by_pred.get(p.id, [])
        if len(states) < 2:
            errors.append(
                f"predicate {p.key!r} has {len(states)} state(s); need ≥2"
            )
            continue
        # State key uniqueness — UQ should already guarantee this, but
        # double-check so a stale UQ in a forked schema is caught here too.
        keys = [s.state_key for s in states]
        if len(set(keys)) != len(keys):
            errors.append(
                f"predicate {p.key!r} has duplicate state_keys: {keys}"
            )
        prior_sum = sum(s.prior_probability for s in states)
        if abs(prior_sum - 1.0) > SUM_TOL:
            errors.append(
                f"predicate {p.key!r} prior_probability sums to {prior_sum:.6f}, "
                f"expected 1.0 (±{SUM_TOL})"
            )

    # Scenario links — must reference a real (predicate, state) pair, and
    # weights must sum to 1 per scenario.
    links_by_scenario: dict[int, list[ScenarioPredicateLink]] = {}
    for ln in db.query(ScenarioPredicateLink).all():
        links_by_scenario.setdefault(ln.scenario_id, []).append(ln)
    valid_pred_states: set[tuple[int, str]] = {
        (s.predicate_id, s.state_key)
        for sts in states_by_pred.values() for s in sts
    }

    for sc in db.query(Scenario).all():
        if not sc.active:
            continue
        links = links_by_scenario.get(sc.id, [])
        if not links:
            errors.append(f"scenario {sc.key!r} has no predicate links")
            continue
        for ln in links:
            if (ln.predicate_id, ln.required_state_key) not in valid_pred_states:
                # Find the predicate key for a friendlier message.
                pred = next(
                    (p for p in predicates if p.id == ln.predicate_id),
                    None,
                )
                pkey = pred.key if pred else f"id={ln.predicate_id}"
                errors.append(
                    f"scenario {sc.key!r} link → predicate {pkey} requires "
                    f"state {ln.required_state_key!r} which doesn't exist"
                )
        wsum = sum(ln.weight for ln in links)
        if abs(wsum - 1.0) > SUM_TOL:
            errors.append(
                f"scenario {sc.key!r} weights sum to {wsum:.6f}, "
                f"expected 1.0 (±{SUM_TOL})"
            )

    # Likelihood table coverage. Posterior compute looks up by
    # (direction, strength_bucket) — a missing combo silently zeroes the
    # update for that evidence shape. Better to fail loudly at seed time.
    have = {
        (lr.direction, lr.strength_bucket): lr.multiplier
        for lr in db.query(EvidenceLikelihoodRatio).all()
    }
    expected_dirs = ("support", "contradict", "neutral")
    expected_strengths = ("weak", "moderate", "strong")
    for d in expected_dirs:
        for s in expected_strengths:
            mult = have.get((d, s))
            if mult is None:
                errors.append(
                    f"evidence_likelihood_ratios missing ({d}, {s})"
                )
            elif mult <= 0:
                errors.append(
                    f"evidence_likelihood_ratios ({d}, {s}) has multiplier "
                    f"{mult} ≤ 0; must be > 0"
                )

    # Required scenario_settings keys — recompute reads these. A missing
    # `default_decay_half_life_days` would silently disable decay.
    required_keys = (
        "default_decay_half_life_days",
        "independence_assumption_acknowledged",
        "min_evidence_for_movement",
    )
    have_keys = {s.key for s in db.query(ScenarioSetting).all()}
    for k in required_keys:
        if k not in have_keys:
            errors.append(f"scenario_settings missing required key {k!r}")

    # Spot-check: at least one source credibility row exists. Not strictly
    # required (CLI defaults credibility=1.0 if no row matches), but a
    # totally empty table almost always means seed didn't run.
    if db.query(SourceCredibilityDefault).count() == 0:
        errors.append("source_credibility_defaults is empty — re-run seed")

    return errors
