"""Pure-math layer for the Scenarios belief engine.

No DB calls. No SQLAlchemy. Inputs are plain dicts / NamedTuples; outputs
are plain dicts. Easy to unit-test, easy to replay against historical data.

The DB-aware orchestration lives in app/scenarios/service.py.

See docs/scenarios/01-foundation.md §"Math" for the derivation.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import NamedTuple


# ─── Inputs ─────────────────────────────────────────────────────────────

class EvidenceInput(NamedTuple):
    """One piece of evidence as the math layer needs it.

    The service layer constructs these from PredicateEvidence rows after
    filtering to confirmed-only and joining the per-row credibility.
    """
    target_state_key: str
    direction: str          # "support" | "contradict" | "neutral"
    strength_bucket: str    # "weak" | "moderate" | "strong"
    credibility: float      # 0–1
    observed_at: datetime


class ScenarioLink(NamedTuple):
    predicate_key: str
    required_state_key: str
    weight: float           # contribution within the scenario; weights sum to 1


class ScenarioInput(NamedTuple):
    key: str
    links: list[ScenarioLink]


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
        weight = math.log(lr) * ev.credibility * decay_factor(
            ev.observed_at, now, half_life_days
        )
        logits[ev.target_state_key] += weight

    return _softmax(logits)


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

    return _softmax_no_renorm_check(log_unnorm)


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
) -> float:
    """Per-evidence log-odds contribution: log(LR) * credibility * decay.

    This is exactly what compute_posterior accumulates onto the target
    state's logit. Surfaced for the evidence drill-down so movement is
    fully attributable: "p1 moved +0.18 because of these specific
    findings, weighted thus."

    Returns 0.0 when the likelihood is missing/invalid (defensive — the
    table is seeded but a future schema drift shouldn't crash the
    dashboard render)."""
    if now is None:
        now = datetime.utcnow()
    lr = likelihood_table.get((direction, strength_bucket))
    if lr is None or lr <= 0:
        return 0.0
    return math.log(lr) * credibility * decay_factor(observed_at, now, half_life_days)


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
