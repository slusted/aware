"""End-to-end verification for the Scenarios belief engine
(docs/scenarios/01-foundation.md).

Runs the spec acceptance criteria against a throwaway SQLite DB —
same harness pattern as scripts/verify_preference_rollup.py. Hermetic.

Usage:
    python scripts/verify_scenarios_math.py
Exit code: 0 on all pass, 1 on any fail.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


_tmp = Path(tempfile.mkdtemp(prefix="scenarios_verify_")) / "verify.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: F401, E402  (registers tables on Base.metadata)
from app.models import (  # noqa: E402
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
from app.scenarios.posterior import (  # noqa: E402
    EvidenceInput,
    ScenarioInput,
    ScenarioLink,
    compute_posterior,
    compute_scenario_probabilities,
    compute_sensitivity,
    decay_factor,
)
from app.scenarios.integrity import validate_seed  # noqa: E402
from app.scenarios import service  # noqa: E402

import scripts.seed_scenarios as seed_module  # noqa: E402
import json  # noqa: E402


# ── Harness ─────────────────────────────────────────────────────────

_passes = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f"  — {detail}"
    print(line)
    if ok:
        _passes += 1
    else:
        _failures.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def almost(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ── Pure math (no DB) ────────────────────────────────────────────────

def _likelihood_table_for_tests() -> dict[tuple[str, str], float]:
    return {
        ("support", "strong"): 3.0,
        ("support", "moderate"): 1.8,
        ("support", "weak"): 1.2,
        ("neutral", "weak"): 1.0,
        ("neutral", "moderate"): 1.0,
        ("neutral", "strong"): 1.0,
        ("contradict", "weak"): 0.8,
        ("contradict", "moderate"): 0.55,
        ("contradict", "strong"): 0.4,
    }


def test_pure_math():
    section("Pure math: posterior, decay, scenarios, sensitivity")
    LR = _likelihood_table_for_tests()
    now = datetime(2026, 5, 4, 12, 0, 0)
    HL = 60  # days

    # Decay function basics.
    check("decay at age 0 == 1.0",
          almost(decay_factor(now, now, HL), 1.0))
    check("decay at one half-life == 0.5",
          almost(decay_factor(now - timedelta(days=HL), now, HL), 0.5, tol=1e-9))
    check("decay at two half-lives == 0.25",
          almost(decay_factor(now - timedelta(days=2 * HL), now, HL), 0.25, tol=1e-9))
    check("decay with half_life<=0 disables (==1.0)",
          almost(decay_factor(now - timedelta(days=365), now, 0), 1.0))
    check("future-dated observed_at clamps to 1.0",
          almost(decay_factor(now + timedelta(days=10), now, HL), 1.0))

    # Prior-only.
    prior_bin = {"a": 0.55, "b": 0.45}
    p = compute_posterior(prior_bin, [], LR, HL, now=now)
    check("prior-only binary returns prior unchanged",
          almost(p["a"], 0.55) and almost(p["b"], 0.45))

    prior_3 = {"x": 0.40, "y": 0.30, "z": 0.30}
    p3 = compute_posterior(prior_3, [], LR, HL, now=now)
    check("prior-only ternary returns prior unchanged",
          almost(p3["x"], 0.40) and almost(p3["y"], 0.30) and almost(p3["z"], 0.30))

    # Strong support on binary moves the right direction.
    ev_strong_a = [EvidenceInput("a", "support", "strong", 1.0, now)]
    pb = compute_posterior(prior_bin, ev_strong_a, LR, HL, now=now)
    check("binary: strong support raises target", pb["a"] > 0.55,
          f"a={pb['a']:.3f}")
    check("binary: posterior sums to 1",
          almost(pb["a"] + pb["b"], 1.0, tol=1e-9))

    # Standard log-odds: posterior_odds(a) = prior_odds(a) * LR
    expected_odds = (0.55 / 0.45) * 3.0
    expected_pa = expected_odds / (1 + expected_odds)
    check("binary: matches log-odds formula (prior_odds * LR)",
          almost(pb["a"], expected_pa, tol=1e-9),
          f"got {pb['a']:.6f}, expected {expected_pa:.6f}")

    # Strong support + strong contradict on the same target ~ prior.
    ev_offset = [
        EvidenceInput("a", "support", "strong", 1.0, now),
        EvidenceInput("a", "contradict", "strong", 1.0, now),
    ]
    po = compute_posterior(prior_bin, ev_offset, LR, HL, now=now)
    # log(3.0) + log(0.4) = log(1.2) — slight net positive on support side.
    expected_net_lr = 3.0 * 0.4
    expected_odds = (0.55 / 0.45) * expected_net_lr
    expected_pa = expected_odds / (1 + expected_odds)
    check("binary: support+contradict net to LR=1.2", almost(po["a"], expected_pa, tol=1e-9))

    # Decay halves log-odds movement at one half-life.
    ev_old = [EvidenceInput("a", "support", "strong", 1.0, now - timedelta(days=HL))]
    p_old = compute_posterior(prior_bin, ev_old, LR, HL, now=now)
    # Expected: log-odds delta = log(3.0) * 1.0 * 0.5
    expected_lor_delta = math.log(3.0) * 0.5
    actual_lor_delta = math.log(p_old["a"] / p_old["b"]) - math.log(0.55 / 0.45)
    check("binary: one-half-life decay halves log-odds delta",
          almost(actual_lor_delta, expected_lor_delta, tol=1e-9),
          f"got {actual_lor_delta:.6f}, expected {expected_lor_delta:.6f}")

    # Credibility 0.5 = half the log-odds movement.
    ev_half = [EvidenceInput("a", "support", "strong", 0.5, now)]
    p_half = compute_posterior(prior_bin, ev_half, LR, HL, now=now)
    expected_lor = math.log(3.0) * 0.5
    actual_lor = math.log(p_half["a"] / p_half["b"]) - math.log(0.55 / 0.45)
    check("binary: credibility 0.5 halves log-odds delta",
          almost(actual_lor, expected_lor, tol=1e-9))

    # Multi-state target: support raises target, lowers others proportionally.
    ev_x = [EvidenceInput("x", "support", "moderate", 1.0, now)]
    p3a = compute_posterior(prior_3, ev_x, LR, HL, now=now)
    check("ternary: target rises", p3a["x"] > 0.40)
    check("ternary: non-targets fall",
          p3a["y"] < 0.30 and p3a["z"] < 0.30)
    # Non-targets shrink proportionally (their ratio stays equal to the
    # ratio of their priors).
    check("ternary: non-target ratio preserved",
          almost(p3a["y"] / p3a["z"], 0.30 / 0.30, tol=1e-9))
    check("ternary: posterior sums to 1",
          almost(sum(p3a.values()), 1.0, tol=1e-9))

    # Neutral evidence is a no-op.
    ev_neutral = [EvidenceInput("a", "neutral", "moderate", 1.0, now)]
    p_neut = compute_posterior(prior_bin, ev_neutral, LR, HL, now=now)
    check("neutral evidence: posterior == prior",
          almost(p_neut["a"], 0.55, tol=1e-9))

    # Evidence referencing an unknown state is silently skipped (config drift).
    ev_drift = [EvidenceInput("zzz", "support", "strong", 1.0, now)]
    p_drift = compute_posterior(prior_bin, ev_drift, LR, HL, now=now)
    check("unknown target state skipped (no crash, no movement)",
          almost(p_drift["a"], 0.55, tol=1e-9))

    # Scenario derivation: when posteriors == priors, scenarios still
    # rank in order of how well their constraints align with priors.
    posteriors = {
        "p1": {"platform": 0.55, "agent": 0.45},
        "p8": {"marketplace": 0.40, "external_interface": 0.25, "workflow": 0.35},
    }
    scenarios = [
        ScenarioInput("a", [ScenarioLink("p1", "platform", 0.5),
                            ScenarioLink("p8", "marketplace", 0.5)]),
        ScenarioInput("b", [ScenarioLink("p1", "agent", 0.5),
                            ScenarioLink("p8", "external_interface", 0.5)]),
    ]
    sp = compute_scenario_probabilities(posteriors, scenarios)
    check("scenarios: probabilities sum to 1",
          almost(sum(sp.values()), 1.0, tol=1e-9))
    check("scenarios: A favored over B from priors", sp["a"] > sp["b"],
          f"a={sp['a']:.3f}, b={sp['b']:.3f}")

    # Sensitivity: bumping a state should move scenarios that depend on it.
    sens_a = compute_sensitivity(posteriors, scenarios, "p1", "platform", delta=0.05)
    check("sensitivity: P(scenario A) rises with P(p1=platform)",
          sens_a["a"] > 0)
    check("sensitivity: P(scenario B) falls with P(p1=platform)",
          sens_a["b"] < 0)


# ── DB-backed (seed + recompute + integrity) ──────────────────────────

def _reset_db():
    """Drop and recreate every Base table. Hermetic per test section."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _load_seed_payload() -> dict:
    p = Path(__file__).resolve().parent / "scenarios_seed.json"
    return json.loads(p.read_text(encoding="utf-8"))


def test_seed_and_integrity():
    section("Seed loader + integrity check")
    _reset_db()

    payload = _load_seed_payload()
    db = SessionLocal()
    try:
        counts = seed_module.seed(db, payload)
        db.commit()
    finally:
        db.close()

    check("seed: 8 predicates", counts["predicates"] == 8,
          f"got {counts['predicates']}")
    # p1=2, p2=2, p3=2, p4=2, p5=3, p6=3, p7=2, p8=3 → 19 states
    check("seed: 19 states", counts["states"] == 19,
          f"got {counts['states']}")
    check("seed: 3 scenarios", counts["scenarios"] == 3)
    check("seed: 15 scenario links (5+5+5)", counts["links"] == 15)
    check("seed: 9 likelihood rows", counts["likelihoods"] == 9)
    check("seed: ≥6 source credibility rows", counts["credibility"] >= 6)
    check("seed: ≥3 settings", counts["settings"] >= 3)

    db = SessionLocal()
    try:
        errors = validate_seed(db)
    finally:
        db.close()
    check("integrity: clean seed has zero errors", errors == [],
          ", ".join(errors) if errors else "")

    # Idempotent re-seed: counts stable, no duplicates.
    db = SessionLocal()
    try:
        before_pred = db.query(Predicate).count()
        before_state = db.query(PredicateState).count()
        before_link = db.query(ScenarioPredicateLink).count()
        seed_module.seed(db, payload)
        db.commit()
        after_pred = db.query(Predicate).count()
        after_state = db.query(PredicateState).count()
        after_link = db.query(ScenarioPredicateLink).count()
    finally:
        db.close()
    check("idempotent: predicate count stable",
          before_pred == after_pred, f"{before_pred} → {after_pred}")
    check("idempotent: state count stable",
          before_state == after_state, f"{before_state} → {after_state}")
    check("idempotent: link count stable",
          before_link == after_link, f"{before_link} → {after_link}")

    # Negative test: corrupt one row and confirm validate_seed catches it.
    db = SessionLocal()
    try:
        st = db.query(PredicateState).first()
        original = st.prior_probability
        st.prior_probability = original + 0.5  # busts the sum-to-1 invariant
        db.commit()
        errors = validate_seed(db)
    finally:
        db.close()
    check("integrity: catches busted prior sum",
          any("prior_probability sums to" in e for e in errors),
          f"errors: {errors}")
    # Restore so subsequent tests don't trip.
    db = SessionLocal()
    try:
        st_restore = db.get(PredicateState, st.id)
        st_restore.prior_probability = original
        db.commit()
    finally:
        db.close()


def test_recompute_no_evidence():
    section("Service: recompute with no evidence")
    db = SessionLocal()
    try:
        out = service.recompute_all(db)
        db.commit()
    finally:
        db.close()

    check("recompute: 8 predicates touched", len(out) == 8,
          f"got {len(out)}")

    # current_probability should equal prior_probability for every state.
    db = SessionLocal()
    try:
        all_match = True
        worst_diff = 0.0
        for s in db.query(PredicateState).all():
            diff = abs(s.current_probability - s.prior_probability)
            worst_diff = max(worst_diff, diff)
            if diff > 1e-9:
                all_match = False
        snap_count = db.query(PredicatePosteriorSnapshot).count()
    finally:
        db.close()
    check("no evidence: cache == prior across all states",
          all_match, f"worst diff {worst_diff}")
    check("no evidence: snapshots written for all 19 states",
          snap_count == 19, f"got {snap_count}")


def test_recompute_with_evidence():
    section("Service: recompute after one strong-support evidence")
    db = SessionLocal()
    try:
        p8 = db.query(Predicate).filter(Predicate.key == "p8").one()
        before = {
            s.state_key: s.current_probability
            for s in db.query(PredicateState)
            .filter(PredicateState.predicate_id == p8.id).all()
        }
        snap_before = db.query(PredicatePosteriorSnapshot).count()

        ev = PredicateEvidence(
            predicate_id=p8.id,
            target_state_key="workflow",
            direction="support",
            strength_bucket="strong",
            credibility=1.0,
            classified_by="manual",
            observed_at=datetime.utcnow(),
            confirmed_at=datetime.utcnow(),
            notes="verify-script test evidence",
        )
        db.add(ev)
        db.flush()

        out = service.recompute_predicate(db, p8.id)
        db.commit()

        after = {
            s.state_key: s.current_probability
            for s in db.query(PredicateState)
            .filter(PredicateState.predicate_id == p8.id).all()
        }
        snap_after = db.query(PredicatePosteriorSnapshot).count()
    finally:
        db.close()

    check("evidence: workflow probability rises",
          after["workflow"] > before["workflow"],
          f"{before['workflow']:.3f} → {after['workflow']:.3f}")
    check("evidence: marketplace falls",
          after["marketplace"] < before["marketplace"])
    check("evidence: external_interface falls",
          after["external_interface"] < before["external_interface"])
    check("evidence: posterior sums to 1",
          almost(sum(after.values()), 1.0, tol=1e-9))

    # Standard Bayes check: P(workflow | e) ∝ prior_workflow * 3.0; others unchanged.
    pw = before["workflow"] * 3.0
    pm = before["marketplace"]
    pe = before["external_interface"]
    z = pw + pm + pe
    expected_workflow = pw / z
    check("evidence: matches Bayes (prior * LR / Z)",
          almost(after["workflow"], expected_workflow, tol=1e-9),
          f"got {after['workflow']:.6f}, expected {expected_workflow:.6f}")

    check("evidence: snapshot rows added (3 new for ternary)",
          snap_after - snap_before == 3,
          f"delta {snap_after - snap_before}")


def test_scenario_probabilities():
    section("Service: live scenario probabilities")
    db = SessionLocal()
    try:
        sp = service.scenario_probabilities(db)
    finally:
        db.close()

    check("scenarios: 3 scenarios returned", len(sp) == 3, f"got {sp}")
    check("scenarios: probabilities sum to 1",
          almost(sum(sp.values()), 1.0, tol=1e-6),
          f"sum={sum(sp.values()):.6f}")
    # After the workflow-favoring evidence, scenario C (workflow dominance)
    # should outrank A (aggregator dominance) — workflow has weight 0.35
    # in C and isn't constrained in A.
    check("scenarios: C (workflow) outranks A (aggregator)",
          sp["c"] > sp["a"],
          f"a={sp['a']:.3f}, b={sp['b']:.3f}, c={sp['c']:.3f}")


# ── Main ────────────────────────────────────────────────────────────

def main() -> int:
    print(f"Verify DB: {_tmp}")
    test_pure_math()
    test_seed_and_integrity()
    test_recompute_no_evidence()
    test_recompute_with_evidence()
    test_scenario_probabilities()

    print(f"\n{_passes} passed, {len(_failures)} failed.")
    if _failures:
        print("Failures:")
        for n in _failures:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
