"""End-to-end verification for the monthly predicate-review pass
(docs/scenarios/06-predicate-review.md).

Hermetic. Stubs the LLM with canned JSON so the pipeline runs without
ANTHROPIC_API_KEY. Same harness pattern as verify_scenarios_math.py.

Usage:
    python scripts/verify_predicate_review.py
Exit code: 0 on all pass, 1 on any fail.
"""
from __future__ import annotations

import json as _json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


_tmp = Path(tempfile.mkdtemp(prefix="predicate_review_verify_")) / "verify.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"
# Make sure the live Anthropic path is never reached even if a key is set
# in the developer's shell — we always inject a stub.
os.environ.pop("ANTHROPIC_API_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: F401, E402
from app.models import (  # noqa: E402
    Finding,
    Predicate,
    PredicateState,
    PredicateEvidence,
    PredicateProposal,
    PredicateReview,
)
from app.scenarios import review as review_mod  # noqa: E402
from app.scenarios import service as svc  # noqa: E402


# ── Harness ─────────────────────────────────────────────────────────────

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


# ── Fixtures ────────────────────────────────────────────────────────────

def _setup_schema():
    Base.metadata.create_all(engine)


def _seed_baseline(db):
    """Two predicates (p1: agent vs platform, p4: pricing power vs commodity)
    with a handful of confirmed evidence rows on p1."""
    p1 = Predicate(
        key="p1", name="Discovery distribution", category="discovery",
        statement="Will buyers prefer platform-led or agent-led discovery?",
        active=True,
    )
    p4 = Predicate(
        key="p4", name="Pricing power", category="transaction",
        statement="Do incumbents retain pricing power against agent intermediaries?",
        active=True,
    )
    db.add_all([p1, p4])
    db.flush()
    db.add_all([
        PredicateState(
            predicate_id=p1.id, state_key="platform", label="Platform-dominant",
            ordinal_position=0, prior_probability=0.5, current_probability=0.5,
        ),
        PredicateState(
            predicate_id=p1.id, state_key="agent", label="Agent-led",
            ordinal_position=1, prior_probability=0.5, current_probability=0.5,
        ),
        PredicateState(
            predicate_id=p4.id, state_key="commodity", label="Commodity",
            ordinal_position=0, prior_probability=0.5, current_probability=0.5,
        ),
        PredicateState(
            predicate_id=p4.id, state_key="premium", label="Premium",
            ordinal_position=1, prior_probability=0.5, current_probability=0.5,
        ),
    ])
    # Findings + evidence on p1 — 5 rows so structural-action thresholds
    # have room to either pass or fail.
    now = datetime.utcnow()
    findings = []
    for i in range(5):
        f = Finding(
            run_id=None,
            competitor=f"Comp{i}",
            source="manual",
            title=f"Title {i}",
            content=f"Body {i}",
            hash=f"hash-{i}",
            created_at=now - timedelta(days=i * 3),
        )
        findings.append(f)
    db.add_all(findings)
    db.flush()

    evidence = []
    for i, f in enumerate(findings):
        ev = PredicateEvidence(
            finding_id=f.id,
            predicate_id=p1.id,
            target_state_key="agent" if i % 2 == 0 else "platform",
            direction="support",
            strength_bucket="moderate",
            credibility=0.9,
            classified_by="manual",
            observed_at=now - timedelta(days=i * 3),
            confirmed_at=now - timedelta(days=i * 3),
            notes=f"note {i}",
        )
        evidence.append(ev)
    db.add_all(evidence)
    db.flush()
    db.commit()
    return p1, p4, evidence


def _stub_factory(canned: dict):
    """Returns an `llm_call(system, user)` function that ignores its
    arguments and returns the canned dict (JSON-encoded). Tests substitute
    this for the live API call."""
    encoded = _json.dumps(canned)

    def _call(_system: str, _user: str) -> str:
        return encoded
    return _call


# ── Tests ───────────────────────────────────────────────────────────────

def main() -> int:
    _setup_schema()

    # ── Threshold gate: refine_statement (pass) ─────────────────────────
    section("Gate: refine_statement, ≥1 supporting finding → proposal created")
    db = SessionLocal()
    p1, p4, evidence = _seed_baseline(db)
    fid_first = evidence[0].finding_id
    stub = _stub_factory({
        "summary_text": "Small wording drift — recommend refine.",
        "decided_no_change": False,
        "fitness_per_evidence": [
            {"evidence_id": evidence[0].id, "fitness": "fits", "read_as": "ok"},
        ],
        "suggested_actions": [
            {
                "kind": "refine_statement",
                "rationale": "wording sharper",
                "payload": {"new_statement": "Will primary discovery happen on a platform UI or via an agent interface?"},
                "supporting_finding_ids": [fid_first],
            }
        ],
    })
    result = review_mod.run_predicate_review(
        db, predicate_keys=["p1"], llm_call=stub,
    )
    check("review reported reviewed=1", result.reviewed == 1, str(result))
    check("review reported wording=1", result.wording == 1, str(result))
    n_proposals = (
        db.query(PredicateProposal)
        .filter(PredicateProposal.kind == "refine_statement")
        .count()
    )
    check("refine_statement proposal created", n_proposals == 1)
    db.close()

    # ── Threshold gate: refine_statement (fail) ─────────────────────────
    section("Gate: refine_statement, 0 supporting findings → dropped")
    db = SessionLocal()
    db.query(PredicateProposal).delete()
    db.query(PredicateReview).delete()
    db.commit()
    stub = _stub_factory({
        "summary_text": "Trying to refine without support.",
        "decided_no_change": False,
        "fitness_per_evidence": [],
        "suggested_actions": [
            {
                "kind": "refine_statement",
                "rationale": "no specifics",
                "payload": {"new_statement": "x"},
                "supporting_finding_ids": [],
            }
        ],
    })
    review_mod.run_predicate_review(db, predicate_keys=["p1"], llm_call=stub)
    n_proposals = db.query(PredicateProposal).count()
    check("zero-support refine_statement was dropped", n_proposals == 0)
    db.close()

    # ── Threshold gate: split_state (fail) ──────────────────────────────
    section("Gate: split_state, 2 misfits → dropped")
    db = SessionLocal()
    db.query(PredicateProposal).delete()
    db.query(PredicateReview).delete()
    db.commit()
    ev_ids = [e.id for e in db.query(PredicateEvidence).filter(PredicateEvidence.predicate_id == p1.id).all()]
    stub = _stub_factory({
        "summary_text": "Considering split with only 2 misfits.",
        "decided_no_change": False,
        "fitness_per_evidence": [
            {"evidence_id": ev_ids[0], "fitness": "misfit", "read_as": "off"},
            {"evidence_id": ev_ids[1], "fitness": "misfit", "read_as": "off"},
            {"evidence_id": ev_ids[2], "fitness": "fits", "read_as": "ok"},
        ],
        "suggested_actions": [
            {
                "kind": "split_state",
                "rationale": "splitting agent into native vs assisted",
                "payload": {"state_key": "agent", "new_states": []},
                "supporting_finding_ids": [
                    db.get(PredicateEvidence, ev_ids[0]).finding_id,
                    db.get(PredicateEvidence, ev_ids[1]).finding_id,
                ],
            }
        ],
    })
    review_mod.run_predicate_review(db, predicate_keys=["p1"], llm_call=stub)
    n_split = db.query(PredicateProposal).filter(PredicateProposal.kind == "split_state").count()
    check("split_state with 2 misfits was dropped", n_split == 0)
    db.close()

    # ── Threshold gate: split_state (pass) ──────────────────────────────
    section("Gate: split_state, 3 misfits → proposal created")
    db = SessionLocal()
    db.query(PredicateProposal).delete()
    db.query(PredicateReview).delete()
    db.commit()
    ev_ids = [e.id for e in db.query(PredicateEvidence).filter(PredicateEvidence.predicate_id == p1.id).all()]
    stub = _stub_factory({
        "summary_text": "Three misfits — split is justified.",
        "decided_no_change": False,
        "fitness_per_evidence": [
            {"evidence_id": ev_ids[0], "fitness": "misfit", "read_as": "off"},
            {"evidence_id": ev_ids[1], "fitness": "misfit", "read_as": "off"},
            {"evidence_id": ev_ids[2], "fitness": "misfit", "read_as": "off"},
        ],
        "suggested_actions": [
            {
                "kind": "split_state",
                "rationale": "split agent",
                "payload": {"state_key": "agent", "new_states": []},
                "supporting_finding_ids": [
                    db.get(PredicateEvidence, ev_ids[0]).finding_id,
                    db.get(PredicateEvidence, ev_ids[1]).finding_id,
                    db.get(PredicateEvidence, ev_ids[2]).finding_id,
                ],
            }
        ],
    })
    review_mod.run_predicate_review(db, predicate_keys=["p1"], llm_call=stub)
    n_split = db.query(PredicateProposal).filter(PredicateProposal.kind == "split_state").count()
    check("split_state with 3 misfits was created", n_split == 1)
    db.close()

    # ── decided_no_change ───────────────────────────────────────────────
    section("decided_no_change semantics")
    db = SessionLocal()
    db.query(PredicateProposal).delete()
    db.query(PredicateReview).delete()
    db.commit()
    ev_ids = [e.id for e in db.query(PredicateEvidence).filter(PredicateEvidence.predicate_id == p1.id).all()]

    # All fits, no actions → decided_no_change=True
    stub = _stub_factory({
        "summary_text": "All clean.",
        "decided_no_change": True,
        "fitness_per_evidence": [
            {"evidence_id": eid, "fitness": "fits", "read_as": ""}
            for eid in ev_ids
        ],
        "suggested_actions": [],
    })
    review_mod.run_predicate_review(db, predicate_keys=["p1"], llm_call=stub)
    last = (
        db.query(PredicateReview)
        .filter(PredicateReview.predicate_id == p1.id)
        .order_by(PredicateReview.id.desc())
        .first()
    )
    check("decided_no_change=True when all fits + no actions", last.decided_no_change is True)

    db.query(PredicateProposal).delete()
    db.query(PredicateReview).delete()
    db.commit()

    # One awkward, no actions → decided_no_change=False
    stub = _stub_factory({
        "summary_text": "One awkward fit.",
        "decided_no_change": True,  # LLM lies; server overrides
        "fitness_per_evidence": [
            {"evidence_id": ev_ids[0], "fitness": "awkward", "read_as": "weird"},
            *[
                {"evidence_id": eid, "fitness": "fits", "read_as": ""}
                for eid in ev_ids[1:]
            ],
        ],
        "suggested_actions": [],
    })
    review_mod.run_predicate_review(db, predicate_keys=["p1"], llm_call=stub)
    last = (
        db.query(PredicateReview)
        .filter(PredicateReview.predicate_id == p1.id)
        .order_by(PredicateReview.id.desc())
        .first()
    )
    check("decided_no_change=False when ≥1 awkward, server overrides", last.decided_no_change is False)
    db.close()

    # ── Supersede same-shape ────────────────────────────────────────────
    section("Supersede: identical refine_statement payload twice")
    db = SessionLocal()
    db.query(PredicateProposal).delete()
    db.query(PredicateReview).delete()
    db.commit()
    fid_first = db.query(PredicateEvidence).first().finding_id
    same = {
        "summary_text": "Same proposal, twice.",
        "decided_no_change": False,
        "fitness_per_evidence": [],
        "suggested_actions": [
            {
                "kind": "refine_statement",
                "rationale": "stable wording",
                "payload": {"new_statement": "stable rewording"},
                "supporting_finding_ids": [fid_first],
            }
        ],
    }
    review_mod.run_predicate_review(db, predicate_keys=["p1"], llm_call=_stub_factory(same))
    review_mod.run_predicate_review(db, predicate_keys=["p1"], llm_call=_stub_factory(same))
    pendings = (
        db.query(PredicateProposal)
        .filter(
            PredicateProposal.kind == "refine_statement",
            PredicateProposal.status == "pending",
        )
        .count()
    )
    superseded = (
        db.query(PredicateProposal)
        .filter(
            PredicateProposal.kind == "refine_statement",
            PredicateProposal.status == "superseded",
        )
        .count()
    )
    check("only one pending after dup", pendings == 1, f"pending={pendings}")
    check("older row marked superseded", superseded == 1, f"superseded={superseded}")
    db.close()

    # ── Apply: refine_statement ────────────────────────────────────────
    section("Apply: accept_proposal(refine_statement) updates predicate.statement")
    db = SessionLocal()
    proposal = (
        db.query(PredicateProposal)
        .filter(
            PredicateProposal.kind == "refine_statement",
            PredicateProposal.status == "pending",
        )
        .first()
    )
    new_stmt = "stable rewording"
    svc.accept_proposal(db, proposal.id, user=None)
    db.commit()
    pred = db.query(Predicate).filter(Predicate.key == "p1").one()
    check(
        "predicate.statement updated by accept",
        pred.statement == new_stmt,
        f"got {pred.statement!r}",
    )
    proposal_after = db.get(PredicateProposal, proposal.id)
    check("proposal status → accepted", proposal_after.status == "accepted")
    db.close()

    # ── Apply: reassign_evidence ───────────────────────────────────────
    section("Apply: accept_proposal(reassign_evidence) moves the row")
    db = SessionLocal()
    db.query(PredicateProposal).delete()
    db.query(PredicateReview).delete()
    db.commit()
    ev = db.query(PredicateEvidence).filter(PredicateEvidence.predicate_id == p1.id).first()
    stub = _stub_factory({
        "summary_text": "One row really belongs on p4.",
        "decided_no_change": False,
        "fitness_per_evidence": [
            {
                "evidence_id": ev.id,
                "fitness": "awkward",
                "read_as": "pricing not distribution",
                "reassign_target_predicate_key": "p4",
            }
        ],
        "suggested_actions": [],
    })
    review_mod.run_predicate_review(db, predicate_keys=["p1"], llm_call=stub)
    reassign = db.query(PredicateProposal).filter(PredicateProposal.kind == "reassign_evidence").first()
    check("reassign_evidence proposal created", reassign is not None)
    if reassign:
        svc.accept_proposal(db, reassign.id, user=None)
        db.commit()
        ev_after = db.get(PredicateEvidence, ev.id)
        new_pred = db.query(Predicate).filter(Predicate.key == "p4").one()
        check("evidence row moved to target predicate", ev_after.predicate_id == new_pred.id)
    db.close()

    # ── Cooldown ───────────────────────────────────────────────────────
    section("Cooldown: next_review_due_at in future skips the predicate")
    db = SessionLocal()
    db.query(PredicateProposal).delete()
    db.query(PredicateReview).delete()
    pred = db.query(Predicate).filter(Predicate.key == "p1").one()
    pred.next_review_due_at = datetime.utcnow() + timedelta(days=30)
    db.commit()
    stub_called = {"n": 0}

    def _counting_stub(system, user):
        stub_called["n"] += 1
        return _json.dumps({
            "summary_text": "x",
            "decided_no_change": True,
            "fitness_per_evidence": [],
            "suggested_actions": [],
        })

    result = review_mod.run_predicate_review(
        db, predicate_keys=["p1"], llm_call=_counting_stub,
    )
    check("cooldown predicate skipped", stub_called["n"] == 0, f"called={stub_called['n']}")
    check("skipped_cooldown counter incremented", result.skipped_cooldown == 1, str(result))
    db.close()

    # ── LLM unset ──────────────────────────────────────────────────────
    section("LLM unset: pipeline returns empty without writing rows")
    db = SessionLocal()
    pred.next_review_due_at = None
    db.merge(pred)
    db.commit()
    db.query(PredicateProposal).delete()
    db.query(PredicateReview).delete()
    db.commit()
    # Pass a stub that mimics "no API key" (returns empty string).
    result = review_mod.run_predicate_review(
        db,
        predicate_keys=["p1"],
        llm_call=lambda s, u: "",
    )
    check("reviewed=0 when LLM returns empty", result.reviewed == 0, str(result))
    n_reviews = db.query(PredicateReview).count()
    check("no PredicateReview rows written", n_reviews == 0)
    db.close()

    # ── Done ───────────────────────────────────────────────────────────
    print(f"\n{_passes} passed, {len(_failures)} failed")
    for f in _failures:
        print(f"  FAILED: {f}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main())
