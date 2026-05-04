"""Idempotent loader for the Scenarios belief engine seed payload.

Originally lived in scripts/seed_scenarios.py and ran only as part of the
Railway release command. The release container on Railway is ephemeral
and (depending on volume-mount config) writes from release commands may
never reach the live SQLite on the persistent volume — which is what
left production with zero predicates after a clean deploy.

Moving the upsert orchestration into the app package lets the web
process call it from `lifespan` (where the volume is guaranteed
mounted), mirroring the pattern app/seed.py::seed_competitors uses.
The CLI script (scripts/seed_scenarios.py) now imports `seed_payload`
from here, so there's a single source of truth for the upsert logic.

Stable keys for upserts:
  predicates by `key`, predicate_states by (predicate_id, state_key),
  scenarios by `key`, scenario_predicate_links by (scenario_id, predicate_id),
  evidence_likelihood_ratios by (direction, strength_bucket),
  source_credibility_defaults by source_type, scenario_settings by key.

First seed sets PredicateState.current_probability = prior_probability
(no evidence yet). Re-seeding only updates prior_probability; accumulated
posterior cache is preserved. Trigger a recompute if you want the cache
to pick up new priors.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import (
    EvidenceLikelihoodRatio,
    Predicate,
    PredicateState,
    Scenario,
    ScenarioPredicateLink,
    ScenarioSetting,
    SourceCredibilityDefault,
)


DEFAULT_SEED_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "scenarios_seed.json"
)


def upsert_predicate(db: Session, p: dict) -> Predicate:
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


def upsert_state(db: Session, predicate_id: int, s: dict) -> PredicateState:
    row = (
        db.query(PredicateState)
        .filter(
            PredicateState.predicate_id == predicate_id,
            PredicateState.state_key == s["state_key"],
        )
        .one_or_none()
    )
    if row is None:
        # First seed: current = prior so unprimed dashboards still render
        # something sensible before any evidence has been logged.
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


def upsert_scenario(db: Session, sc: dict) -> Scenario:
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


def upsert_scenario_link(
    db: Session, scenario_id: int, predicate_id: int, ln: dict
) -> ScenarioPredicateLink:
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


def upsert_likelihood(db: Session, lr: dict) -> EvidenceLikelihoodRatio:
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


def upsert_credibility(db: Session, sc: dict) -> SourceCredibilityDefault:
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


def upsert_setting(db: Session, s: dict) -> ScenarioSetting:
    row = db.query(ScenarioSetting).filter(ScenarioSetting.key == s["key"]).one_or_none()
    if row is None:
        row = ScenarioSetting(key=s["key"], value=s["value"])
        db.add(row)
        db.flush()
    else:
        row.value = s["value"]
        row.updated_at = datetime.utcnow()
    return row


def seed_payload(db: Session, payload: dict) -> dict[str, int]:
    """Run every upsert. Returns a count summary the caller can log/print."""
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


def seed_from_default_json(db: Session) -> dict[str, int]:
    """Load the bundled scripts/scenarios_seed.json and apply it.

    Caller owns commit/rollback so this is composable with other
    boot-time seed steps. Raises if the JSON is missing or malformed.
    """
    payload = json.loads(DEFAULT_SEED_PATH.read_text(encoding="utf-8"))
    return seed_payload(db, payload)
