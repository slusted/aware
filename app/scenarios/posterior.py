"""Pure-math layer for the Scenarios belief engine.

No DB calls. No SQLAlchemy. Inputs are plain dicts / NamedTuples; outputs
are plain dicts. Easy to unit-test, easy to replay against historical data.

The DB-aware orchestration lives in app/scenarios/service.py.

See docs/scenarios/01-foundation.md §"Math" for the derivation.
"""
from __future__ import annotations

import math
import os
from datetime import datetime
from typing import NamedTuple


# ─── Inputs ─────────────────────────────────────────────────────────────

class EvidenceInput(NamedTuple):
    """One piece of evidence as the math layer needs it.

    The service layer constructs these from PredicateEvidence rows after
    filtering to confirmed-only and joining the per-row credibility.

    Stage 7 fields (mechanism_present .. redundancy_score) are all
    optional and default to None. None → posterior math substitutes a
    neutral multiplier (=1.0), so legacy / unscored rows behave exactly
    as they did pre-Stage 7.
    """
    target_state_key: str
    direction: str          # "support" | "contradict" | "neutral"
    strength_bucket: str    # "weak" | "moderate" | "strong"
    credibility: float      # 0–1
    observed_at: datetime
    # ── Stage 7 multi-pass scorer fields (all optional) ───────────────
    mechanism_present: str | None = None        # "yes" | "no"
    base_rate_bucket: str | None = None         # "high" | "medium" | "low"
    counter_evidence_strength: str | None = None  # "none" | "weak" | "strong"
    incentive_bias: str | None = None           # "+" | "-" | "0"
    redundancy_score: float | None = None       # 0–1 (1 = near-duplicate)


class ScenarioLink(NamedTuple):
    predicate_key: str
    required_state_key: str
    weight: float           # contribution within the scenario; weights sum to 1


class ScenarioInput(NamedTuple):
    key: str
    links: list[ScenarioLink]


# ─── Stage 7: scorer-driven multipliers + per-evidence cap ─────────────
#
# The Bayesian update was prior_logit + log(LR) * credibility * decay.
# Stage 7 adds four additional multiplicative scalars so that a single
# Haiku-bucketed strong-support can be dampened by the Sonnet scorer
# when mechanism is absent, base rate is low, a strong counter-reading
# exists, the source has incentive bias, or this is a near-duplicate of
# a recent finding.
#
# The multiplier tables are deliberately small. They live in code (not
# DB) until the user has tuned them at least once — promote to a
# `scenario_settings` row when there's something worth tuning.
#
# A None value is the legacy / unscored case: substitute 1.0 so behavior
# matches pre-Stage 7 exactly.

_MECHANISM_MULT: dict[str | None, float] = {
    "yes": 1.0,
    "no": 0.6,
    None: 1.0,
}

_BASE_RATE_MULT: dict[str | None, float] = {
    "high": 1.2,
    "medium": 1.0,
    "low": 0.7,
    None: 1.0,
}

_COUNTER_MULT: dict[str | None, float] = {
    "none": 1.0,
    "weak": 0.85,
    "strong": 0.5,
    None: 1.0,
}

_INCENTIVE_MULT: dict[str | None, float] = {
    "+": 0.85,    # source benefits if claim is believed
    "-": 0.85,    # source benefits if claim is disbelieved
    "0": 1.0,     # neutral
    None: 1.0,
}

# Per-evidence cap on |Δ logit| applied to the target state. Caps after
# multiplier composition; sign-preserving so contradictory evidence stays
# contradictory.
#
# Default = log(10) ≈ 2.303 — deliberately permissive ("pretty high") so
# the cap doesn't silently dampen normal evidence today. Strong-support
# at peak amplification (log(3) × cred 1.0 × decay 1.0 × base_rate=high
# 1.2 ≈ 1.32) is well below the cap; the cap only kicks in if dozens of
# findings stack on a single state in a window short enough that decay
# hasn't bled them off, which is the safety case it exists for.
#
# Override via SCENARIOS_MAX_LOGIT_DELTA env var (raw float). The value
# is exposed publicly (no leading underscore) and rendered in the math
# view footer so a user can see exactly what cap their posteriors were
# computed against.
def _read_cap() -> float:
    raw = os.environ.get("SCENARIOS_MAX_LOGIT_DELTA")
    if not raw:
        return math.log(10.0)
    try:
        v = float(raw)
        if v <= 0:
            return math.log(10.0)
        return v
    except ValueError:
        return math.log(10.0)


MAX_ABS_LOGIT_DELTA = _read_cap()


# ─── Posterior probability ceiling ──────────────────────────────────────
#
# With no ceiling, a steady drip of supportive evidence pushes a
# predicate's posterior asymptotically to 1.0 — arithmetically impossible
# for new contradicting evidence to ever flip back, and visually
# misleading on the dashboard. True Bayesian forecasting reserves
# residual probability for "the evidence model itself could be wrong" /
# "we're not seeing every relevant signal." A soft cap of 0.99 (i.e.
# 1% residual on the losing state) operationalises that humility.
#
# Override via SCENARIOS_MAX_POSTERIOR env var. Clamped to (0.5, 1.0)
# to prevent foot-guns. 1.0 disables the ceiling entirely.

def _read_max_posterior() -> float:
    raw = os.environ.get("SCENARIOS_MAX_POSTERIOR")
    if not raw:
        return 0.99
    try:
        v = float(raw)
        if v <= 0.5 or v > 1.0:
            return 0.99
        return v
    except ValueError:
        return 0.99


MAX_POSTERIOR = _read_max_posterior()


def apply_probability_ceiling(probs: dict[str, float]) -> dict[str, float]:
    """Cap any state at MAX_POSTERIOR; redistribute the excess
    proportionally across the remaining states so the distribution
    still sums to 1.0.

    Single-pass is sufficient: at most one state can exceed
    MAX_POSTERIOR at a time (they sum to 1), and bringing that state
    down only adds mass elsewhere. Public so the scenario layer can
    apply the same cap to scenario probabilities.
    """
    if not probs or MAX_POSTERIOR >= 1.0:
        return probs
    top_key = max(probs, key=probs.get)
    top_p = probs[top_key]
    if top_p <= MAX_POSTERIOR:
        return probs
    rest_total = 1.0 - top_p
    if rest_total <= 0:
        others = [k for k in probs if k != top_key]
        if not others:
            return probs
        share = (1.0 - MAX_POSTERIOR) / len(others)
        return {k: (MAX_POSTERIOR if k == top_key else share) for k in probs}
    scale = (1.0 - MAX_POSTERIOR) / rest_total
    return {k: (MAX_POSTERIOR if k == top_key else probs[k] * scale)
            for k in probs}


def _redundancy_multiplier(score: float | None) -> float:
    """Linear penalty: redundancy 0 → 1.0 (no penalty), 1 → 0.5
    (half-weight). Out-of-range / None values fall back to 1.0 so a
    bad embedding can't crash the math. Capped to [0.5, 1.0] so a
    rogue >1.0 score can't amplify weight."""
    if score is None:
        return 1.0
    s = max(0.0, min(1.0, float(score)))
    return 1.0 - 0.5 * s


def _scorer_multiplier(ev: "EvidenceInput") -> float:
    """Compose all Stage-7 multipliers into one scalar. None fields
    contribute 1.0 (neutral)."""
    return (
        _MECHANISM_MULT.get(ev.mechanism_present, 1.0)
        * _BASE_RATE_MULT.get(ev.base_rate_bucket, 1.0)
        * _COUNTER_MULT.get(ev.counter_evidence_strength, 1.0)
        * _INCENTIVE_MULT.get(ev.incentive_bias, 1.0)
        * _redundancy_multiplier(ev.redundancy_score)
    )


def cap_logit_delta(delta: float) -> float:
    """Sign-preserving cap on a single evidence's logit contribution.
    Public so dashboard / math view can render the same value; tests can
    reference MAX_ABS_LOGIT_DELTA by name."""
    if delta > MAX_ABS_LOGIT_DELTA:
        return MAX_ABS_LOGIT_DELTA
    if delta < -MAX_ABS_LOGIT_DELTA:
        return -MAX_ABS_LOGIT_DELTA
    return delta


# ─── Decay ──────────────────────────────────────────────────────────────

def decay_factor(observed_at: datetime, now: datetime, half_life_days: float) -> float:
    """Exponential half-life decay. Returns 1.0 at age 0, 0.5 at one
    half-life, 0.25 at two half-lives, etc. Future-dated `observed_at`
    clamps to 1.0 (no super-charging from clock skew).

    half_life_days <= 0 disables decay (returns 1.0). Useful for tests
    or when the operator wants pure cumulative evidence.
    """
    if half_life_days is None or half_life_days <= 0:
        return 1.0
    age_days = (now - observed_at).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    # 0.5 ** (age / half_life) is exp(-ln(2) * age / half_life). Same thing,
    # one fewer call into math.
    return 0.5 ** (age_days / half_life_days)


# ─── Per-predicate posterior ────────────────────────────────────────────

def compute_posterior(
    prior: dict[str, float],
    evidence: list[EvidenceInput],
    likelihood_table: dict[tuple[str, str], float],
    half_life_days: float,
    now: datetime | None = None,
) -> dict[str, float]:
    """Bayesian posterior over the predicate's states given evidence.

    Each evidence applies a likelihood ratio LR(direction, strength) to
    its target state only:

        logit(target) += log(LR) * credibility * decay
        # other states unchanged — softmax over priors does redistribution

    For binary predicates this is exactly the standard log-odds form. For
    N-state predicates, mass redistributes across non-target states in
    proportion to their existing probabilities (no ordinal awareness).

    Returns a dict of the same shape as `prior`, summing to 1.0.
    """
    if not prior:
        return {}
    if now is None:
        now = datetime.utcnow()

    # Initial logits from priors. Any zero-prior state stays at -inf so it
    # can never receive mass — that's the right behavior for "ruled out".
    logits: dict[str, float] = {}
    for state_key, p in prior.items():
        logits[state_key] = math.log(p) if p > 0 else float("-inf")

    for ev in evidence:
        if ev.target_state_key not in logits:
            # Evidence references a state that doesn't exist for this
            # predicate (config drift, e.g. state was renamed). Skip
            # silently — the service layer logs the count of skipped rows
            # so we can spot it without crashing the recompute.
            continue
        lr = likelihood_table.get((ev.direction, ev.strength_bucket))
        if lr is None or lr <= 0:
            continue
        # Stage 7: log(LR) × credibility × decay × scorer multipliers,
        # then capped to ±log(2) so one finding can't dominate. None-
        # valued scorer fields collapse to 1.0 so legacy rows are
        # mathematically unchanged.
        weight = (
            math.log(lr)
            * ev.credibility
            * decay_factor(ev.observed_at, now, half_life_days)
            * _scorer_multiplier(ev)
        )
        logits[ev.target_state_key] += cap_logit_delta(weight)

    return apply_probability_ceiling(_softmax(logits))


def _softmax(logits: dict[str, float]) -> dict[str, float]:
    """Numerically stable softmax. Subtracts the max logit before
    exponentiating so we don't overflow on large positive logits."""
    finite = [v for v in logits.values() if v != float("-inf")]
    if not finite:
        # All states ruled out — degenerate, return uniform over inputs.
        n = len(logits)
        return {k: 1.0 / n for k in logits}
    m = max(finite)
    exps = {
        k: math.exp(v - m) if v != float("-inf") else 0.0
        for k, v in logits.items()
    }
    z = sum(exps.values())
    if z == 0:
        n = len(logits)
        return {k: 1.0 / n for k in logits}
    return {k: v / z for k, v in exps.items()}


# ─── Scenario probability ───────────────────────────────────────────────

def compute_scenario_probabilities(
    predicate_posteriors: dict[str, dict[str, float]],
    scenarios: list[ScenarioInput],
) -> dict[str, float]:
    """Derive P(scenario) under independence:

        unnormalized(S) = Π_j  P(P_j = required_state_j) ^ weight_j
        P(S_k) = unnormalized(S_k) / Σ_m unnormalized(S_m)

    Independence is an explicit known limitation — see spec §"Known
    limitations". Stage 4 surfaces it in the UI.

    Skips links whose predicate or required state isn't present in
    `predicate_posteriors` (e.g. predicate inactive, schema drift). A
    scenario with no resolvable links contributes 0 to the
    normalization and ends up at probability 0.
    """
    if not scenarios:
        return {}

    log_unnorm: dict[str, float] = {}
    for sc in scenarios:
        log_score = 0.0
        any_resolved = False
        for link in sc.links:
            pred = predicate_posteriors.get(link.predicate_key)
            if pred is None:
                continue
            p = pred.get(link.required_state_key)
            if p is None:
                continue
            if p <= 0:
                # Required state has zero probability — scenario is impossible.
                log_score = float("-inf")
                any_resolved = True
                break
            log_score += link.weight * math.log(p)
            any_resolved = True
        log_unnorm[sc.key] = log_score if any_resolved else float("-inf")

    return apply_probability_ceiling(_softmax_no_renorm_check(log_unnorm))


def _softmax_no_renorm_check(log_unnorm: dict[str, float]) -> dict[str, float]:
    """Same as _softmax but tolerates all-(-inf) by returning all zeros
    rather than uniform. A scenario set where nothing resolves should
    surface as 0 across the board, not as a fake uniform distribution."""
    finite = [v for v in log_unnorm.values() if v != float("-inf")]
    if not finite:
        return {k: 0.0 for k in log_unnorm}
    m = max(finite)
    exps = {
        k: math.exp(v - m) if v != float("-inf") else 0.0
        for k, v in log_unnorm.items()
    }
    z = sum(exps.values())
    if z == 0:
        return {k: 0.0 for k in log_unnorm}
    return {k: v / z for k, v in exps.items()}


# ─── Sensitivity ────────────────────────────────────────────────────────

def compute_sensitivity(
    predicate_posteriors: dict[str, dict[str, float]],
    scenarios: list[ScenarioInput],
    predicate_key: str,
    target_state_key: str,
    delta: float = 0.05,
) -> dict[str, float]:
    """Numerical ∂P(scenario) / ∂P(predicate = state).

    Bumps the named predicate's probability for the named state by ±delta,
    rebalances the other states proportionally to keep the predicate sum
    at 1.0, then recomputes scenario probabilities and reports the per-
    scenario deltas (high − low) / (2 * delta).

    Returns dict[scenario_key, sensitivity]. Positive = scenario gets
    more likely as that state's probability rises. Magnitude indicates
    how much the scenario depends on this specific predicate state.

    Cheap and accurate enough for stage-1 dashboards. Caller can drop
    delta to 0.01 for finer reads at the cost of more compute.
    """
    pred = predicate_posteriors.get(predicate_key)
    if pred is None or target_state_key not in pred:
        return {sc.key: 0.0 for sc in scenarios}

    high = _bumped(predicate_posteriors, predicate_key, target_state_key, +delta)
    low = _bumped(predicate_posteriors, predicate_key, target_state_key, -delta)

    p_high = compute_scenario_probabilities(high, scenarios)
    p_low = compute_scenario_probabilities(low, scenarios)

    return {
        sc.key: (p_high.get(sc.key, 0.0) - p_low.get(sc.key, 0.0)) / (2 * delta)
        for sc in scenarios
    }


# ─── Dashboard math (Stage 3) ───────────────────────────────────────────

def shannon_entropy(probabilities: dict[str, float]) -> float:
    """Normalized Shannon entropy over a probability distribution.

    Returns a float in [0, 1]:
      0 = one state has all the mass (no contention; the engine is "sure")
      1 = uniform distribution (maximum contention; the engine is balanced)

    Normalized by log(N) where N is the number of states with non-zero
    probability, so a 2-state and 3-state predicate are directly
    comparable. Sorts the dashboard's "most contested" view.
    """
    if not probabilities:
        return 0.0
    n = sum(1 for p in probabilities.values() if p > 0)
    if n <= 1:
        return 0.0  # single non-zero state = certainty
    h_raw = 0.0
    for p in probabilities.values():
        if p > 0:
            h_raw -= p * math.log(p)
    h_max = math.log(n)
    if h_max <= 0:
        return 0.0
    return min(1.0, max(0.0, h_raw / h_max))


def velocity_pp(baseline: float, current: float) -> float:
    """Signed percentage-point delta. Trivial wrapper, lives here so the
    Stage-3 dashboard imports all its math from one place."""
    return (current - baseline) * 100.0


def log_odds_contribution(
    direction: str,
    strength_bucket: str,
    credibility: float,
    observed_at: datetime,
    likelihood_table: dict[tuple[str, str], float],
    half_life_days: float,
    now: datetime | None = None,
    *,
    mechanism_present: str | None = None,
    base_rate_bucket: str | None = None,
    counter_evidence_strength: str | None = None,
    incentive_bias: str | None = None,
    redundancy_score: float | None = None,
) -> float:
    """Per-evidence log-odds contribution as it actually lands in the
    posterior — log(LR) × credibility × decay × scorer multipliers,
    capped at ±log(2). Exactly what compute_posterior accumulates onto
    the target state's logit. Surfaced for the evidence drill-down so
    movement is fully attributable: "p1 moved +0.18 because of these
    specific findings, weighted thus."

    Stage-7 scorer fields are keyword-only and default to None so legacy
    callers (without scorer columns) get the pre-Stage-7 value
    unchanged — None collapses each multiplier to 1.0.

    Returns 0.0 when the likelihood is missing/invalid (defensive — the
    table is seeded but a future schema drift shouldn't crash the
    dashboard render)."""
    if now is None:
        now = datetime.utcnow()
    lr = likelihood_table.get((direction, strength_bucket))
    if lr is None or lr <= 0:
        return 0.0
    raw = (
        math.log(lr)
        * credibility
        * decay_factor(observed_at, now, half_life_days)
        * _MECHANISM_MULT.get(mechanism_present, 1.0)
        * _BASE_RATE_MULT.get(base_rate_bucket, 1.0)
        * _COUNTER_MULT.get(counter_evidence_strength, 1.0)
        * _INCENTIVE_MULT.get(incentive_bias, 1.0)
        * _redundancy_multiplier(redundancy_score)
    )
    return cap_logit_delta(raw)


def downsample_series(
    series: list,
    max_points: int = 100,
) -> list:
    """Sample a long snapshot series down to roughly `max_points` evenly
    spaced entries. Always keeps the first and last so endpoints are
    honest; walks every Nth entry in between. Cheap O(N) — no smoothing,
    no averaging, just decimation. Acceptable for sparkline rendering at
    the densities the snapshot table reaches.

    Series elements are opaque to this function — typically tuples of
    `(timestamp, probability)` but anything subscriptable will do.

    Returns the input unchanged if it's already at or under the cap.
    """
    if len(series) <= max_points:
        return series
    step = len(series) / max_points
    out = []
    i = 0.0
    while int(i) < len(series):
        out.append(series[int(i)])
        i += step
    if out and out[-1] is not series[-1]:
        out.append(series[-1])
    return out


def _bumped(
    posteriors: dict[str, dict[str, float]],
    predicate_key: str,
    target_state_key: str,
    delta: float,
) -> dict[str, dict[str, float]]:
    """Return a copy of `posteriors` with one state's probability shifted by
    `delta` and the remaining states' mass scaled to keep the sum at 1.0.
    Clamps to [0, 1] so a delta beyond the available mass is just pinned."""
    out = {k: dict(v) for k, v in posteriors.items()}
    pred = out[predicate_key]
    cur = pred[target_state_key]
    new_target = max(0.0, min(1.0, cur + delta))
    rest_total = sum(v for k, v in pred.items() if k != target_state_key)
    new_rest_total = 1.0 - new_target
    if rest_total <= 0:
        # Nothing else to rebalance; assign new_target and split remainder
        # uniformly across the other states.
        others = [k for k in pred if k != target_state_key]
        share = new_rest_total / len(others) if others else 0.0
        for k in others:
            pred[k] = share
    else:
        scale = new_rest_total / rest_total
        for k in pred:
            if k == target_state_key:
                pred[k] = new_target
            else:
                pred[k] = pred[k] * scale
    return out
