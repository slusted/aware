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
    PredicateProposal,
    Scenario,
    ScenarioPredicateLink,
    EvidenceLikelihoodRatio,
    SourceCredibilityDefault,
    ScenarioSetting,
    User,
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
            mechanism_present=r.mechanism_present,
            base_rate_bucket=r.base_rate_bucket,
            counter_evidence_strength=r.counter_evidence_strength,
            incentive_bias=r.incentive_bias,
            redundancy_score=r.redundancy_score,
            evidence_under_alt_bucket=r.evidence_under_alt_bucket,
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


# ─── Authoring writes (Stage 5: assumption controls) ───────────────────
#
# These mutate the same predicate/scenario tables the seed loader writes
# to. They share the seed loader's invariants: per-predicate priors must
# sum to 1.0, per-scenario weights must sum to 1.0, and every scenario
# link's required_state_key must reference a real PredicateState. We
# validate per-row instead of running the full integrity.validate_seed
# so a single edit doesn't fail because some other unrelated row is
# already broken.
#
# Scope this stage: edit fields and parameters of EXISTING predicates /
# scenarios / states / links. Adding or removing rows is deliberately out
# of scope until we have UI for the create flow.
#
# Caller owns the transaction. Functions raise ValueError on validation
# failure so the route layer can surface a 400 with the message and the
# DB stays untouched.

# Reuse the same float-arithmetic tolerance the integrity validator uses,
# so prior/weight sums authored as 0.55 + 0.45 don't fail at 1.0000…001.
_SUM_TOL = 1e-6


def update_predicate(
    db: Session,
    predicate_key: str,
    *,
    name: str | None = None,
    statement: str | None = None,
    category: str | None = None,
    active: bool | None = None,
    decay_half_life_days: int | None = None,
    decay_half_life_days_explicit: bool = False,
    states: list[dict] | None = None,
) -> Predicate:
    """Apply an authoring edit to one existing predicate.

    All field args are optional — None means "leave alone". The exception
    is `decay_half_life_days`: passing None is ambiguous between "no
    change" and "clear the override and fall back to the global default",
    so callers that intend to clear the override must additionally set
    `decay_half_life_days_explicit=True`.

    `states`, when supplied, must list every existing state of the
    predicate keyed by `state_key`. Each entry may include any of
    {label, prior_probability, ordinal_position}; missing keys leave the
    existing value alone. Adding or removing states is rejected — that's
    a future story (would invalidate confirmed evidence rows pointing at
    the old state_key).

    Raises ValueError on validation failure: unknown predicate, unknown
    state_key, prior sum outside [1.0 ± SUM_TOL], or fewer than 2 active
    states implied.
    """
    pred = (
        db.query(Predicate)
        .filter(Predicate.key == predicate_key)
        .one_or_none()
    )
    if pred is None:
        raise ValueError(f"predicate not found: {predicate_key!r}")

    if name is not None:
        pred.name = name
    if statement is not None:
        pred.statement = statement
    if category is not None:
        pred.category = category
    if active is not None:
        pred.active = active
    if decay_half_life_days_explicit:
        pred.decay_half_life_days = decay_half_life_days
    pred.updated_at = datetime.utcnow()

    if states is not None:
        existing = (
            db.query(PredicateState)
            .filter(PredicateState.predicate_id == pred.id)
            .all()
        )
        by_key = {s.state_key: s for s in existing}
        # Check the supplied list mentions only existing state_keys.
        unknown = [s["state_key"] for s in states if s["state_key"] not in by_key]
        if unknown:
            raise ValueError(
                f"unknown state_key(s) for predicate {predicate_key!r}: "
                f"{unknown}. Adding/removing states is not supported via "
                "update_predicate."
            )
        for s in states:
            row = by_key[s["state_key"]]
            if "label" in s and s["label"] is not None:
                row.label = s["label"]
            if "ordinal_position" in s and s["ordinal_position"] is not None:
                row.ordinal_position = int(s["ordinal_position"])
            if "prior_probability" in s and s["prior_probability"] is not None:
                row.prior_probability = float(s["prior_probability"])
            row.updated_at = datetime.utcnow()

        # Validate prior sum across ALL states (including untouched ones)
        # so partial edits can't drift the predicate out of [0, 1].
        prior_sum = sum(r.prior_probability for r in existing)
        if abs(prior_sum - 1.0) > _SUM_TOL:
            raise ValueError(
                f"predicate {predicate_key!r} prior_probability would sum to "
                f"{prior_sum:.6f}, expected 1.0 (±{_SUM_TOL})"
            )

    if pred.active:
        # Active predicates need ≥2 states or scenario derivation breaks.
        n_states = (
            db.query(PredicateState)
            .filter(PredicateState.predicate_id == pred.id)
            .count()
        )
        if n_states < 2:
            raise ValueError(
                f"predicate {predicate_key!r} would have {n_states} state(s); "
                "active predicates need at least 2."
            )

    db.flush()
    return pred


def update_scenario(
    db: Session,
    scenario_key: str,
    *,
    name: str | None = None,
    description: str | None = None,
    active: bool | None = None,
    links: list[dict] | None = None,
) -> Scenario:
    """Apply an authoring edit to one existing scenario.

    All args are optional. `links`, when supplied, must list every
    existing link of the scenario keyed by `predicate_key`. Each entry
    may include `weight` and/or `required_state_key`; missing keys leave
    the existing value alone. Adding or removing links is rejected.

    Raises ValueError on validation failure: unknown scenario, unknown
    predicate_key in a link, required_state_key not a real state of that
    predicate, or weight sum outside [1.0 ± SUM_TOL].
    """
    sc = (
        db.query(Scenario)
        .filter(Scenario.key == scenario_key)
        .one_or_none()
    )
    if sc is None:
        raise ValueError(f"scenario not found: {scenario_key!r}")

    if name is not None:
        sc.name = name
    if description is not None:
        sc.description = description
    if active is not None:
        sc.active = active
    sc.updated_at = datetime.utcnow()

    if links is not None:
        existing_links = (
            db.query(ScenarioPredicateLink)
            .filter(ScenarioPredicateLink.scenario_id == sc.id)
            .all()
        )
        # Index existing links by their predicate's key (not predicate_id)
        # so the API can speak in stable identifiers.
        pred_id_to_key = {p.id: p.key for p in db.query(Predicate).all()}
        by_pkey = {pred_id_to_key.get(ln.predicate_id): ln for ln in existing_links}
        by_pkey.pop(None, None)

        unknown = [
            ln["predicate_key"] for ln in links
            if ln.get("predicate_key") not in by_pkey
        ]
        if unknown:
            raise ValueError(
                f"unknown predicate_key(s) for scenario {scenario_key!r}: "
                f"{unknown}. Adding/removing links is not supported via "
                "update_scenario."
            )

        # Pre-load every (predicate_id, state_key) pair so we can validate
        # required_state_key changes without a per-link query.
        valid_pred_states = {
            (s.predicate_id, s.state_key)
            for s in db.query(PredicateState).all()
        }

        for ln in links:
            row = by_pkey[ln["predicate_key"]]
            if "weight" in ln and ln["weight"] is not None:
                row.weight = float(ln["weight"])
            if "required_state_key" in ln and ln["required_state_key"] is not None:
                if (row.predicate_id, ln["required_state_key"]) not in valid_pred_states:
                    raise ValueError(
                        f"scenario {scenario_key!r} link → predicate "
                        f"{ln['predicate_key']!r}: required_state_key "
                        f"{ln['required_state_key']!r} is not a state of "
                        "that predicate."
                    )
                row.required_state_key = ln["required_state_key"]

        weight_sum = sum(r.weight for r in existing_links)
        if abs(weight_sum - 1.0) > _SUM_TOL:
            raise ValueError(
                f"scenario {scenario_key!r} weights would sum to "
                f"{weight_sum:.6f}, expected 1.0 (±{_SUM_TOL})"
            )

    db.flush()
    return sc


# ─── Stage 6: predicate-review proposal accept / reject ────────────────
#
# Accept dispatches to the same authoring functions the chat tool and the
# admin UI already use (update_predicate / direct evidence mutation).
# That keeps mutation logic in one place and means the audit trail —
# Predicate.updated_at, snapshot history — is unchanged.
#
# Reject is non-mutating: just status + decided_at + decided_by + reason.
#
# The kinds merge_with and new_predicate are reserved by the schema but
# explicitly out of scope this stage. We raise ValueError on accept
# rather than silently doing nothing.


def accept_proposal(
    db: Session,
    proposal_id: int,
    user: User | None,
    *,
    now: datetime | None = None,
) -> PredicateProposal:
    """Apply a pending PredicateProposal by dispatching to the matching
    authoring path. Marks the proposal `accepted` and stamps
    `decided_by` + `decided_at`.

    Raises ValueError on:
      - unknown proposal id
      - proposal not in `pending` status (no double-accept)
      - reserved kind (merge_with / new_predicate)
      - downstream validation failure from the authoring function
    """
    import json as _json
    from ..models import PredicateProposal as _PP, PredicateState as _PS

    if now is None:
        now = datetime.utcnow()
    p = db.get(_PP, proposal_id)
    if p is None:
        raise ValueError(f"proposal {proposal_id} not found")
    if p.status != "pending":
        raise ValueError(
            f"proposal {proposal_id} is {p.status!r}, only pending can be accepted"
        )
    if p.kind in ("merge_with", "new_predicate"):
        raise ValueError(
            f"proposal kind {p.kind!r} is reserved for the future global "
            "queue — not accepted via this endpoint."
        )

    try:
        payload = _json.loads(p.target_payload_json or "{}")
    except _json.JSONDecodeError as e:
        raise ValueError(f"proposal {proposal_id} payload is not valid JSON: {e}")

    pkey = p.source_predicate_key
    if p.kind == "refine_statement":
        if not pkey:
            raise ValueError("refine_statement requires source_predicate_key")
        new_statement = payload.get("new_statement")
        if not new_statement or not isinstance(new_statement, str):
            raise ValueError("refine_statement payload missing 'new_statement' string")
        update_predicate(db, pkey, statement=new_statement)
    elif p.kind == "rename_state":
        if not pkey:
            raise ValueError("rename_state requires source_predicate_key")
        sk = payload.get("state_key")
        new_label = payload.get("new_label")
        if not sk or not isinstance(new_label, str):
            raise ValueError(
                "rename_state payload missing 'state_key' or 'new_label'"
            )
        update_predicate(
            db, pkey,
            states=[{"state_key": sk, "label": new_label}],
        )
    elif p.kind == "reorder_states":
        if not pkey:
            raise ValueError("reorder_states requires source_predicate_key")
        order = payload.get("order")
        if not isinstance(order, list) or not order:
            raise ValueError("reorder_states payload missing 'order' list")
        states_payload = [
            {"state_key": sk, "ordinal_position": i}
            for i, sk in enumerate(order)
        ]
        update_predicate(db, pkey, states=states_payload)
    elif p.kind == "split_state":
        # Authoring split is not supported by update_predicate yet
        # (adding/removing states is rejected there). Spec acknowledges
        # this — surface the proposal but require manual follow-up.
        raise ValueError(
            "split_state cannot be auto-applied — current update_predicate "
            "rejects state additions. Use 'Refine in chat' to author the "
            "split, then accept this proposal as a reject (or via SQL)."
        )
    elif p.kind == "retire":
        if not pkey:
            raise ValueError("retire requires source_predicate_key")
        update_predicate(db, pkey, active=False)
    elif p.kind == "reassign_evidence":
        ev_id = payload.get("evidence_id")
        target_pkey = payload.get("to_predicate_key")
        if not ev_id or not target_pkey:
            raise ValueError(
                "reassign_evidence payload missing 'evidence_id' or 'to_predicate_key'"
            )
        ev = db.get(PredicateEvidence, ev_id)
        if ev is None:
            raise ValueError(f"evidence {ev_id} not found")
        new_pred = (
            db.query(Predicate).filter(Predicate.key == target_pkey).one_or_none()
        )
        if new_pred is None:
            raise ValueError(f"target predicate {target_pkey!r} not found")
        # Default the target_state_key to the first ordinal state of the
        # new predicate when the payload doesn't specify one. The agent
        # will usually pin it in payload['to_state_key'], but if not, we
        # fall back so the FK remains valid.
        target_sk = payload.get("to_state_key")
        if not target_sk:
            first = (
                db.query(_PS)
                .filter(_PS.predicate_id == new_pred.id)
                .order_by(_PS.ordinal_position)
                .first()
            )
            target_sk = first.state_key if first else ev.target_state_key
        old_predicate_id = ev.predicate_id
        ev.predicate_id = new_pred.id
        ev.target_state_key = target_sk
        # Recompute both predicates so posteriors reflect the move. The
        # math layer is commutative — recompute order doesn't matter.
        recompute_predicate(db, old_predicate_id, now=now)
        recompute_predicate(db, new_pred.id, now=now)
    else:
        raise ValueError(f"unknown proposal kind: {p.kind!r}")

    p.status = "accepted"
    p.decided_at = now
    p.decided_by = user.id if user else None
    db.flush()
    return p


def reject_proposal(
    db: Session,
    proposal_id: int,
    user: User | None,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> PredicateProposal:
    """Mark a pending proposal `rejected`. No predicate / evidence
    mutation happens. Subsequent monthly runs may re-propose the same
    shape — that's fine; the analyst can reject again."""
    if now is None:
        now = datetime.utcnow()
    p = db.get(PredicateProposal, proposal_id)
    if p is None:
        raise ValueError(f"proposal {proposal_id} not found")
    if p.status != "pending":
        raise ValueError(
            f"proposal {proposal_id} is {p.status!r}, only pending can be rejected"
        )
    p.status = "rejected"
    p.decided_at = now
    p.decided_by = user.id if user else None
    p.decision_reason = (reason or "").strip() or None
    db.flush()
    return p
