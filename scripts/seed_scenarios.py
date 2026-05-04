"""Idempotent seed loader for the Scenarios belief engine.

Reads scripts/scenarios_seed.json and upserts every row by stable key:
  predicates by `key`, predicate_states by (predicate_id, state_key),
  scenarios by `key`, scenario_predicate_links by (scenario_id, predicate_id),
  evidence_likelihood_ratios by (direction, strength_bucket),
  source_credibility_defaults by source_type, scenario_settings by key.

Safe to re-run. After upsert, runs app/scenarios/integrity.validate_seed
and exits non-zero on any violation.

First seed sets PredicateState.current_probability = prior_probability
(no evidence yet). Re-seeding only updates prior_probability — accumulated
posterior cache is preserved. Trigger a recompute if you want the cache
to pick up new priors.

Usage:
    python -m scripts.seed_scenarios
    python -m scripts.seed_scenarios --json path/to/custom_seed.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Make `app.*` importable when run as a module from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Predicate,
    PredicateState,
    Scenario,
    ScenarioPredicateLink,
    EvidenceLikelihoodRatio,
    SourceCredibilityDefault,
    ScenarioSetting,
)
from app.scenarios.integrity import validate_seed  # noqa: E402


DEFAULT_JSON = Path(__file__).resolve().parent / "scenarios_seed.json"


def upsert_predicate(db, p: dict) -> Predicate:
    row = db.query(Predicate).filter(Predicate.key == p["key"]).one_or_none()
    if row is None:
        row = Predicate(
            key=p["key"],
            name=p["name"],
            statement=p["statement"],
            category=p["category"],
            decay_half_life_days=p.get("decay_half_life_days"),
            active=p.get("active", True),
        )
        db.add(row)
        db.flush()
    else:
        row.name = p["name"]
        row.statement = p["statement"]
        row.category = p["category"]
        row.decay_half_life_days = p.get("decay_half_life_days")
        row.active = p.get("active", True)
        row.updated_at = datetime.utcnow()
    return row


def upsert_state(db, predicate_id: int, s: dict) -> PredicateState:
    row = (
        db.query(PredicateState)
        .filter(
            PredicateState.predicate_id == predicate_id,
            PredicateState.state_key == s["state_key"],
        )
        .one_or_none()
    )
    if row is None:
        # First seed for this state — current = prior so unprimed
        # dashboards still render something sensible.
        row = PredicateState(
            predicate_id=predicate_id,
            state_key=s["state_key"],
            label=s["label"],
            ordinal_position=s["ordinal_position"],
            prior_probability=s["prior_probability"],
            current_probability=s["prior_probability"],
        )
        db.add(row)
        db.flush()
    else:
        # Re-seed: update authoring fields, leave current_probability
        # alone (recompute owns it).
        row.label = s["label"]
        row.ordinal_position = s["ordinal_position"]
        row.prior_probability = s["prior_probability"]
        row.updated_at = datetime.utcnow()
    return row


def upsert_scenario(db, sc: dict) -> Scenario:
    row = db.query(Scenario).filter(Scenario.key == sc["key"]).one_or_none()
    if row is None:
        row = Scenario(
            key=sc["key"],
            name=sc["name"],
            description=sc.get("description", ""),
            active=sc.get("active", True),
        )
        db.add(row)
        db.flush()
    else:
        row.name = sc["name"]
        row.description = sc.get("description", "")
        row.active = sc.get("active", True)
        row.updated_at = datetime.utcnow()
    return row


def upsert_scenario_link(db, scenario_id: int, predicate_id: int, ln: dict) -> ScenarioPredicateLink:
    row = (
        db.query(ScenarioPredicateLink)
        .filter(
            ScenarioPredicateLink.scenario_id == scenario_id,
            ScenarioPredicateLink.predicate_id == predicate_id,
        )
        .one_or_none()
    )
    if row is None:
        row = ScenarioPredicateLink(
            scenario_id=scenario_id,
            predicate_id=predicate_id,
            required_state_key=ln["required_state_key"],
            weight=ln["weight"],
        )
        db.add(row)
        db.flush()
    else:
        row.required_state_key = ln["required_state_key"]
        row.weight = ln["weight"]
    return row


def upsert_likelihood(db, lr: dict) -> EvidenceLikelihoodRatio:
    row = (
        db.query(EvidenceLikelihoodRatio)
        .filter(
            EvidenceLikelihoodRatio.direction == lr["direction"],
            EvidenceLikelihoodRatio.strength_bucket == lr["strength_bucket"],
        )
        .one_or_none()
    )
    if row is None:
        row = EvidenceLikelihoodRatio(
            direction=lr["direction"],
            strength_bucket=lr["strength_bucket"],
            multiplier=lr["multiplier"],
        )
        db.add(row)
        db.flush()
    else:
        row.multiplier = lr["multiplier"]
        row.updated_at = datetime.utcnow()
    return row


def upsert_credibility(db, sc: dict) -> SourceCredibilityDefault:
    row = (
        db.query(SourceCredibilityDefault)
        .filter(SourceCredibilityDefault.source_type == sc["source_type"])
        .one_or_none()
    )
    if row is None:
        row = SourceCredibilityDefault(
            source_type=sc["source_type"],
            credibility=sc["credibility"],
        )
        db.add(row)
        db.flush()
    else:
        row.credibility = sc["credibility"]
        row.updated_at = datetime.utcnow()
    return row


def upsert_setting(db, s: dict) -> ScenarioSetting:
    row = db.query(ScenarioSetting).filter(ScenarioSetting.key == s["key"]).one_or_none()
    if row is None:
        row = ScenarioSetting(key=s["key"], value=s["value"])
        db.add(row)
        db.flush()
    else:
        row.value = s["value"]
        row.updated_at = datetime.utcnow()
    return row


def seed(db, payload: dict) -> dict[str, int]:
    """Run every upsert. Returns a count summary the CLI can print."""
    counts = {
        "predicates": 0, "states": 0,
        "scenarios": 0, "links": 0,
        "likelihoods": 0, "credibility": 0, "settings": 0,
    }

    pred_key_to_id: dict[str, int] = {}
    for p in payload.get("predicates", []):
        pred = upsert_predicate(db, p)
        counts["predicates"] += 1
        pred_key_to_id[pred.key] = pred.id
        for s in p.get("states", []):
            upsert_state(db, pred.id, s)
            counts["states"] += 1

    for sc in payload.get("scenarios", []):
        scenario = upsert_scenario(db, sc)
        counts["scenarios"] += 1
        for ln in sc.get("links", []):
            pid = pred_key_to_id.get(ln["predicate_key"])
            if pid is None:
                # Predicate referenced by a scenario but not declared in the
                # seed payload. integrity.validate_seed will surface this
                # as a friendlier error; here we just skip the link rather
                # than crashing mid-load.
                continue
            upsert_scenario_link(db, scenario.id, pid, ln)
            counts["links"] += 1

    for lr in payload.get("evidence_likelihood_ratios", []):
        upsert_likelihood(db, lr)
        counts["likelihoods"] += 1
    for sc in payload.get("source_credibility_defaults", []):
        upsert_credibility(db, sc)
        counts["credibility"] += 1
    for s in payload.get("scenario_settings", []):
        upsert_setting(db, s)
        counts["settings"] += 1

    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--json", default=str(DEFAULT_JSON),
        help=f"Seed JSON path (default: {DEFAULT_JSON})",
    )
    ap.add_argument(
        "--skip-validate", action="store_true",
        help="Skip integrity.validate_seed after upsert (debug only).",
    )
    args = ap.parse_args()

    payload_path = Path(args.json)
    if not payload_path.is_file():
        print(f"ERROR: seed file not found: {payload_path}", file=sys.stderr)
        return 2
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    db = SessionLocal()
    try:
        counts = seed(db, payload)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("Seed applied:")
    for k, v in counts.items():
        print(f"  {k:>13s}: {v}")

    if args.skip_validate:
        print("Skipped validate_seed (--skip-validate set).")
        return 0

    db = SessionLocal()
    try:
        errors = validate_seed(db)
    finally:
        db.close()

    if errors:
        print("\nINTEGRITY CHECK FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("Integrity check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
