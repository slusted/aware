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


# ── Stage-2: classifier output validation (no real API call) ─────────────

class _StubAnthropicResponse:
    """Mimics what anthropic.Anthropic.messages.create returns. The
    classifier reads .content[0].text and .usage.{input,output}_tokens.
    Both attrs work as plain objects so SimpleNamespace would do too —
    spelled out for clarity."""
    def __init__(self, json_text: str):
        class _Block:
            text = json_text
        self.content = [_Block()]
        class _Usage:
            input_tokens = 100
            output_tokens = 60
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0
        self.usage = _Usage()


class _StubAnthropicClient:
    """One canned reply per call, queue-style. Tests push expected JSON
    onto the queue before invoking the classifier."""
    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls = 0

        class _Messages:
            def __init__(inner):
                inner._cw_wrapped = False

            def create(inner, *args, **kwargs):
                self.calls += 1
                if not self._replies:
                    return _StubAnthropicResponse('{"evidence": []}')
                return _StubAnthropicResponse(self._replies.pop(0))
        self.messages = _Messages()


def test_classifier_parsing():
    section("Stage 2: classifier output validation (stubbed Anthropic)")
    from app.scenarios.classifier import (
        build_roster,
        parse_response,
        build_system_prompt,
        classify_finding,
    )

    # Build a roster matching the seed shape (subset).
    roster = build_roster([
        {"key": "p1", "name": "...", "statement": "...", "category": "discovery",
         "states": [{"state_key": "platform", "label": "Platform"},
                    {"state_key": "agent", "label": "Agent"}]},
        {"key": "p8", "name": "...", "statement": "...", "category": "control_point",
         "states": [{"state_key": "marketplace", "label": "Marketplace"},
                    {"state_key": "external_interface", "label": "External"},
                    {"state_key": "workflow", "label": "Workflow"}]},
    ])

    # Pure parse() unit tests first — no API involved.
    valid_one = '{"evidence": [{"predicate_key": "p1", "target_state_key": "agent", "direction": "support", "strength_bucket": "moderate", "confidence": 0.7, "reasoning": "x"}]}'
    out = parse_response(valid_one, roster)
    check("parse: valid 1-entry returns one ProposedEvidence", len(out) == 1)
    check("parse: predicate_key + target_state preserved",
          out and out[0].predicate_key == "p1" and out[0].target_state_key == "agent")

    valid_two = '{"evidence": [{"predicate_key": "p1", "target_state_key": "agent", "direction": "support", "strength_bucket": "moderate", "confidence": 0.7, "reasoning": "x"}, {"predicate_key": "p8", "target_state_key": "workflow", "direction": "support", "strength_bucket": "weak", "confidence": 0.5, "reasoning": "y"}]}'
    out = parse_response(valid_two, roster)
    check("parse: valid 2-entry returns two", len(out) == 2)

    overshoot = '{"evidence": [{"predicate_key": "p1", "target_state_key": "agent", "direction": "support", "strength_bucket": "moderate", "confidence": 0.7, "reasoning": "a"}, {"predicate_key": "p1", "target_state_key": "platform", "direction": "support", "strength_bucket": "weak", "confidence": 0.5, "reasoning": "b"}, {"predicate_key": "p8", "target_state_key": "workflow", "direction": "support", "strength_bucket": "weak", "confidence": 0.5, "reasoning": "c"}]}'
    out = parse_response(overshoot, roster)
    check("parse: overshoot capped at 2", len(out) == 2)

    bad_predicate = '{"evidence": [{"predicate_key": "p99", "target_state_key": "agent", "direction": "support", "strength_bucket": "moderate", "confidence": 0.7, "reasoning": "x"}]}'
    out = parse_response(bad_predicate, roster)
    check("parse: unknown predicate skipped", out == [])

    bad_state = '{"evidence": [{"predicate_key": "p1", "target_state_key": "fictional", "direction": "support", "strength_bucket": "moderate", "confidence": 0.7, "reasoning": "x"}]}'
    out = parse_response(bad_state, roster)
    check("parse: unknown state skipped", out == [])

    bad_direction = '{"evidence": [{"predicate_key": "p1", "target_state_key": "agent", "direction": "wibble", "strength_bucket": "moderate", "confidence": 0.7, "reasoning": "x"}]}'
    out = parse_response(bad_direction, roster)
    check("parse: invalid direction skipped", out == [])

    nope = "this is not JSON at all"
    out = parse_response(nope, roster)
    check("parse: garbage returns []", out == [])

    fenced = '```json\n{"evidence": [{"predicate_key": "p1", "target_state_key": "agent", "direction": "support", "strength_bucket": "moderate", "confidence": 0.7, "reasoning": "x"}]}\n```'
    out = parse_response(fenced, roster)
    check("parse: tolerates code fences", len(out) == 1)

    # End-to-end with stub client + writes to UsageEvent.
    from datetime import datetime as _dt
    from app.models import UsageEvent
    db = SessionLocal()
    try:
        before_usage = db.query(UsageEvent).count()
    finally:
        db.close()

    stub = _StubAnthropicClient([
        '{"evidence": [{"predicate_key": "p1", "target_state_key": "agent", "direction": "support", "strength_bucket": "strong", "confidence": 0.9, "reasoning": "stub"}]}',
    ])
    finding = {
        "id": 1, "competitor": "TestCo", "source": "manual",
        "signal_type": "product_launch",
        "title": "stub finding", "summary": "stub", "content": "stub",
        "published_at": None, "created_at": _dt.utcnow(),
    }
    sysp = build_system_prompt(roster)
    out = classify_finding(finding, roster, system_prompt=sysp, client=stub)
    check("classify_finding: stub returns one ProposedEvidence",
          len(out) == 1 and out[0].predicate_key == "p1")
    check("classify_finding: stub client was called once", stub.calls == 1)

    db = SessionLocal()
    try:
        after_usage = db.query(UsageEvent).count()
        last = db.query(UsageEvent).order_by(UsageEvent.id.desc()).first()
    finally:
        db.close()
    check("classify_finding: writes one UsageEvent", after_usage - before_usage == 1)
    check("classify_finding: UsageEvent extra.caller is set",
          last is not None
          and isinstance(last.extra, dict)
          and last.extra.get("caller") == "scenarios_classifier")


# ── Stage-2: sweep idempotency + budget guard ────────────────────────────

def test_sweep_idempotency():
    section("Stage 2: sweep idempotency + budget guard")
    from app.scenarios import sweep as sweep_mod
    from app.scenarios.classifier import classify_finding as real_classify
    from app.models import Finding as _Finding

    # Sandbox: insert 3 fresh findings (no scenarios_classified_at).
    db = SessionLocal()
    try:
        from datetime import datetime as _dt
        for i in range(3):
            db.add(_Finding(
                competitor="StubCo",
                source="manual",
                title=f"sweep test {i}",
                summary=f"sweep test {i}",
                hash=f"sweepteststub{i}",
                created_at=_dt.utcnow(),
            ))
        db.commit()
        unclassified_before = (
            db.query(_Finding).filter(_Finding.scenarios_classified_at.is_(None)).count()
        )
    finally:
        db.close()
    check("sweep setup: at least 3 unclassified findings",
          unclassified_before >= 3, f"got {unclassified_before}")

    # Run sweep with a stub classifier_fn that always proposes p1=agent
    # (so we can check evidence rows actually land).
    from app.scenarios.classifier import ProposedEvidence

    def stub_classifier_fn(finding_dict, roster, **kwargs):
        return [ProposedEvidence(
            predicate_key="p1",
            target_state_key="agent",
            direction="support",
            strength_bucket="moderate",
            confidence=0.7,
            reasoning="stub-sweep",
        )]

    db = SessionLocal()
    try:
        result1 = sweep_mod.classify_unclassified(
            db, limit=10, classifier_fn=stub_classifier_fn,
        )
    finally:
        db.close()
    check(
        "sweep: processes >=3 findings",
        result1.findings_processed >= 3,
        f"processed={result1.findings_processed}",
    )
    check(
        "sweep: proposes >=3 evidence rows",
        result1.evidence_proposed >= 3,
        f"proposed={result1.evidence_proposed}",
    )

    # Re-run — every previously-classified finding should be skipped now.
    db = SessionLocal()
    try:
        result2 = sweep_mod.classify_unclassified(
            db, limit=10, classifier_fn=stub_classifier_fn,
        )
    finally:
        db.close()
    check(
        "sweep: re-run processes 0 findings (idempotent)",
        result2.findings_processed == 0,
        f"processed={result2.findings_processed}",
    )

    # Budget guard: pretend yesterday's spend filled the bucket. We do
    # this by setting the env to 0.0 and confirming sweep skips. Insert
    # a fresh unclassified finding so there's something to skip.
    import os as _os
    db = SessionLocal()
    try:
        from datetime import datetime as _dt
        db.add(_Finding(
            competitor="BudgetCo",
            source="manual",
            title="budget probe",
            summary="budget probe",
            hash="budgetprobesweep",
            created_at=_dt.utcnow(),
        ))
        db.commit()
    finally:
        db.close()

    prev_budget = _os.environ.get("SCENARIOS_CLASSIFIER_DAILY_BUDGET_USD")
    _os.environ["SCENARIOS_CLASSIFIER_DAILY_BUDGET_USD"] = "0.00001"
    try:
        db = SessionLocal()
        try:
            result3 = sweep_mod.classify_unclassified(
                db, limit=10, classifier_fn=stub_classifier_fn,
            )
        finally:
            db.close()
    finally:
        if prev_budget is None:
            _os.environ.pop("SCENARIOS_CLASSIFIER_DAILY_BUDGET_USD", None)
        else:
            _os.environ["SCENARIOS_CLASSIFIER_DAILY_BUDGET_USD"] = prev_budget

    check(
        "sweep: budget guard blocks new classification (0 processed when over cap)",
        result3.findings_processed == 0 and result3.skipped_budget >= 1,
        f"processed={result3.findings_processed}, skipped_budget={result3.skipped_budget}",
    )


# ── Stage-2: confirm / reject route logic ────────────────────────────────

def test_confirm_reject_flow():
    section("Stage 2: confirm + reject affect recompute correctly")
    from datetime import datetime as _dt
    from app.scenarios.service import recompute_predicate

    # Create one LLM-proposed (unconfirmed) evidence row directly. Verify
    # recompute leaves the predicate at prior. Confirm. Verify recompute
    # moves the predicate. Reject. Verify recompute returns to prior.
    db = SessionLocal()
    try:
        p1 = db.query(Predicate).filter(Predicate.key == "p1").one()
        # Wipe any p1 evidence + reset cache so we start clean.
        db.query(PredicateEvidence).filter(PredicateEvidence.predicate_id == p1.id).delete()
        for s in db.query(PredicateState).filter(PredicateState.predicate_id == p1.id).all():
            s.current_probability = s.prior_probability
        db.commit()
        prior_agent = next(
            s.prior_probability for s in
            db.query(PredicateState).filter(PredicateState.predicate_id == p1.id).all()
            if s.state_key == "agent"
        )
        ev = PredicateEvidence(
            predicate_id=p1.id,
            target_state_key="agent",
            direction="support",
            strength_bucket="strong",
            credibility=1.0,
            classified_by="llm",
            observed_at=_dt.utcnow(),
            confirmed_at=None,
            notes="route-flow stub",
        )
        db.add(ev)
        db.commit()

        # Recompute with unconfirmed evidence — should be a no-op.
        recompute_predicate(db, p1.id)
        db.commit()
        agent_after_unconfirmed = next(
            s.current_probability for s in
            db.query(PredicateState).filter(PredicateState.predicate_id == p1.id).all()
            if s.state_key == "agent"
        )
    finally:
        db.close()
    check(
        "unconfirmed evidence: posterior == prior (no contribution)",
        almost(agent_after_unconfirmed, prior_agent, tol=1e-9),
        f"prior={prior_agent:.4f} after={agent_after_unconfirmed:.4f}",
    )

    # Confirm and recompute.
    db = SessionLocal()
    try:
        ev = (
            db.query(PredicateEvidence)
            .filter(PredicateEvidence.notes == "route-flow stub")
            .one()
        )
        ev.confirmed_at = _dt.utcnow()
        db.commit()
        recompute_predicate(db, ev.predicate_id)
        db.commit()
        p1 = db.query(Predicate).filter(Predicate.key == "p1").one()
        agent_after_confirmed = next(
            s.current_probability for s in
            db.query(PredicateState).filter(PredicateState.predicate_id == p1.id).all()
            if s.state_key == "agent"
        )
    finally:
        db.close()
    check(
        "confirmed evidence: posterior moved (agent > prior)",
        agent_after_confirmed > prior_agent + 1e-6,
        f"prior={prior_agent:.4f} after={agent_after_confirmed:.4f}",
    )

    # Reject (soft) and recompute → back to prior.
    db = SessionLocal()
    try:
        ev = (
            db.query(PredicateEvidence)
            .filter(PredicateEvidence.notes == "route-flow stub")
            .one()
        )
        ev.classified_by = "user_rejected"
        ev.confirmed_at = None
        db.commit()
        recompute_predicate(db, ev.predicate_id)
        db.commit()
        p1 = db.query(Predicate).filter(Predicate.key == "p1").one()
        agent_after_rejected = next(
            s.current_probability for s in
            db.query(PredicateState).filter(PredicateState.predicate_id == p1.id).all()
            if s.state_key == "agent"
        )
    finally:
        db.close()
    check(
        "rejected evidence: posterior back to prior",
        almost(agent_after_rejected, prior_agent, tol=1e-9),
        f"prior={prior_agent:.4f} after={agent_after_rejected:.4f}",
    )


# ── Main ────────────────────────────────────────────────────────────

def main() -> int:
    print(f"Verify DB: {_tmp}")
    test_pure_math()
    test_seed_and_integrity()
    test_recompute_no_evidence()
    test_recompute_with_evidence()
    test_scenario_probabilities()
    test_classifier_parsing()
    test_sweep_idempotency()
    test_confirm_reject_flow()

    print(f"\n{_passes} passed, {len(_failures)} failed.")
    if _failures:
        print("Failures:")
        for n in _failures:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
