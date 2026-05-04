"""Chat tool catalog for the Scenarios belief engine
(docs/scenarios/04-scenario-dashboard.md §"Chat tools").

Each handler is a thin wrapper around an `app/scenarios/dashboard.py`
or `service.py` function. Hard rules:

  1. No DB queries here. Handlers call exactly one service function and
     turn its return value into a dict.
  2. No business logic. If a handler does anything beyond shape/type
     conversion, the work belongs in the service layer.
  3. JSON-friendly returns. Service functions return NamedTuples; we
     `_asdict()` them so the chat dispatcher's truncation + serialisation
     handles datetimes uniformly.

Registered into the global TOOLS list in app/chat/tools.py via a single
import + extend.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .. import jobs
from ..chat.tools import Tool
from ..models import User
from . import dashboard as dash
from . import proposer as prop
from . import review as review_mod
from . import service as svc


def _nt_list(items) -> list[dict]:
    """List of NamedTuples → list of dicts. Datetimes survive intact."""
    return [item._asdict() for item in items]


# ── handlers ────────────────────────────────────────────────────────────

def _h_list_scenarios(db: Session, user: User, **_: Any) -> dict:
    """Current state of every active scenario, sorted by probability desc."""
    summaries = dash.scenario_summary(db)
    return {
        "results": _nt_list(summaries),
        "total": len(summaries),
        "independence_assumption": True,
    }


def _h_get_scenario_detail(
    db: Session, user: User, *, scenario_key: str, **_: Any,
) -> dict:
    """Full per-scenario breakdown: contributing predicates + sensitivity."""
    detail = dash.scenario_detail(db, scenario_key)
    if detail is None:
        return {"error": f"scenario not found or inactive: {scenario_key!r}"}
    return {
        "summary": detail.summary._asdict(),
        "contributions": _nt_list(detail.contributions),
        "sensitivity_to_predicates": _nt_list(detail.sensitivity_to_predicates),
    }


def _h_list_predicates(db: Session, user: User, **_: Any) -> dict:
    """Mirrors the Predicates tab. Drops sparkline series from the chat
    payload (chat agent can call get_predicate_detail if it needs history)."""
    summaries = dash.predicate_summary(db)
    out: list[dict] = []
    for s in summaries:
        d = s._asdict()
        d.pop("sparkline_dominant", None)
        d["states"] = [st._asdict() for st in s.states]
        out.append(d)
    return {
        "results": out,
        "total": len(out),
        "independence_assumption": True,
    }


def _h_get_predicate_detail(
    db: Session, user: User, *, predicate_key: str, **_: Any,
) -> dict:
    """Per-predicate evidence drill-down with live log-odds contributions.
    Confirmed evidence sorted by |contribution| desc."""
    detail = dash.predicate_detail(db, predicate_key)
    if detail is None:
        return {"error": f"predicate not found or inactive: {predicate_key!r}"}
    summary = detail.summary._asdict()
    summary.pop("sparkline_dominant", None)
    summary["states"] = [st._asdict() for st in detail.summary.states]
    return {
        "summary": summary,
        "evidence_confirmed": _nt_list(detail.evidence_confirmed),
        "evidence_pending": _nt_list(detail.evidence_pending),
        "evidence_rejected": _nt_list(detail.evidence_rejected),
    }


def _h_get_evidence_for_finding(
    db: Session, user: User, *, finding_id: int, **_: Any,
) -> dict:
    """All predicate-evidence rows attached to one finding, with live
    log-odds contributions."""
    rows = dash.evidence_for_finding(db, finding_id)
    return {
        "finding_id": finding_id,
        "results": _nt_list(rows),
        "total": len(rows),
    }


# ── write handlers (Stage 5: assumption controls) ──────────────────────
#
# Both handlers call into the service layer's update_* functions which
# do the validation. We catch ValueError so the chat dispatcher gets a
# friendly {"error": ...} dict rather than a 500.

def _h_update_predicate(
    db: Session,
    user: User,
    *,
    predicate_key: str,
    name: str | None = None,
    statement: str | None = None,
    category: str | None = None,
    active: bool | None = None,
    decay_half_life_days: Any = "__unset__",
    states: list[dict] | None = None,
    **_: Any,
) -> dict:
    """Apply authoring edits to one predicate. Returns the post-edit
    summary so the chat agent can read back what landed."""
    # JSON has no way to distinguish "I want to clear the override
    # (set NULL)" from "I'm not changing this field" — the chat schema
    # would send `null` for both. Treat the sentinel "__unset__" as
    # "no change"; explicit null then unambiguously clears the override.
    explicit = decay_half_life_days != "__unset__"
    half_life = None if not explicit else decay_half_life_days
    try:
        svc.update_predicate(
            db, predicate_key,
            name=name, statement=statement, category=category, active=active,
            decay_half_life_days=half_life,
            decay_half_life_days_explicit=explicit,
            states=states,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        return {"error": str(e)}
    detail = dash.predicate_detail(db, predicate_key)
    if detail is None:
        return {"error": f"predicate {predicate_key!r} disappeared after update"}
    summary = detail.summary._asdict()
    summary.pop("sparkline_dominant", None)
    summary["states"] = [st._asdict() for st in detail.summary.states]
    return {"updated": True, "summary": summary}


def _h_predicate_propose(
    db: Session,
    user: User,
    *,
    key: str,
    name: str,
    statement: str,
    category: str,
    states: list[dict],
    source_finding_ids: list[int],
    reason: str = "",
    **_: Any,
) -> dict:
    """Persist a single LLM-proposed predicate inline from chat.

    Same provenance shape as a batch proposer run: source='llm_proposed',
    proposal_metadata carries finding_ids + reason + model + timestamp.
    Lands in /predicates?source=llm_proposed where the reviewer can
    promote or reject. Validation errors return as {"error": ...} so the
    Agent surfaces them rather than 500ing.
    """
    pred, err = prop.persist_proposal(
        db,
        {
            "key": key,
            "name": name,
            "statement": statement,
            "category": category,
            "states": states,
            "source_finding_ids": source_finding_ids,
            "reason": reason,
        },
        model="agent",
    )
    if err or not pred:
        return {"error": err or "unknown failure persisting proposal"}
    return {
        "proposed": True,
        "predicate": {
            "key": pred.key,
            "name": pred.name,
            "statement": pred.statement,
            "category": pred.category,
            "source": pred.source,
            "url": f"/scenarios/predicates/{pred.key}",
        },
        "review_url": "/predicates?source=llm_proposed",
    }


def _h_run_predicate_proposer(
    db: Session,
    user: User,
    *,
    finding_window_days: int | None = None,
    finding_limit: int | None = None,
    max_proposals: int | None = None,
    **_: Any,
) -> dict:
    """Enqueue a batch predicate-proposal Run (the Phase 3b background
    job). Returns the Run id + queue position so the Agent can tell the
    user what to watch for. Run output streams into /runs/<id> as
    RunEvent log lines."""
    args = {}
    if finding_window_days is not None:
        args["finding_window_days"] = int(finding_window_days)
    if finding_limit is not None:
        args["finding_limit"] = int(finding_limit)
    if max_proposals is not None:
        args["max_proposals"] = int(max_proposals)

    run = jobs.enqueue_run(
        db,
        "predicate_proposal",
        triggered_by="manual",
        job_args=args,
    )
    return {
        "queued": True,
        "kind": "predicate_proposal",
        "run_id": run.id,
        "queue_position": jobs.queue_position(db, run.id),
        "review_url": "/predicates?source=llm_proposed",
        "run_url": f"/runs/{run.id}",
    }


def _h_update_scenario(
    db: Session,
    user: User,
    *,
    scenario_key: str,
    name: str | None = None,
    description: str | None = None,
    active: bool | None = None,
    links: list[dict] | None = None,
    **_: Any,
) -> dict:
    """Apply authoring edits to one scenario. Returns the post-edit
    detail so the chat agent can read back what landed."""
    try:
        svc.update_scenario(
            db, scenario_key,
            name=name, description=description, active=active, links=links,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        return {"error": str(e)}
    detail = dash.scenario_detail(db, scenario_key)
    if detail is None:
        return {"error": f"scenario {scenario_key!r} disappeared after update"}
    return {
        "updated": True,
        "summary": detail.summary._asdict(),
        "contributions": _nt_list(detail.contributions),
    }


# ── Stage 6: predicate-review tools ────────────────────────────────────


def _h_run_predicate_review(
    db: Session,
    user: User,
    *,
    predicate_keys: list[str] | None = None,
    **_: Any,
) -> dict:
    """Enqueue an on-demand predicate-review run. Same path the
    /scenarios "Re-review now" button uses. Returns the queue position
    so the chat can read back what the user just kicked off."""
    run = jobs.enqueue_run(
        db,
        "predicate_review",
        triggered_by="manual",
        job_args={"predicate_keys": predicate_keys} if predicate_keys else None,
    )
    return {
        "queued": True,
        "kind": "predicate_review",
        "run_id": run.id,
        "queue_position": jobs.queue_position(db, run.id),
        "scope": predicate_keys or "all",
    }


def _h_list_pending_proposals(
    db: Session,
    user: User,
    *,
    predicate_key: str | None = None,
    **_: Any,
) -> dict:
    """List predicate-review proposals still in `pending` status —
    optionally scoped to one predicate. Read-only."""
    proposals = review_mod.pending_proposals_for(db, predicate_key)
    return {
        "predicate_key": predicate_key,
        "results": [p._asdict() for p in proposals],
        "total": len(proposals),
    }


def _h_decide_proposal(
    db: Session,
    user: User,
    *,
    proposal_id: int,
    decision: str,
    reason: str | None = None,
    **_: Any,
) -> dict:
    """Accept or reject one pending proposal. Calls the same service-
    layer functions the HTTP routes use. Validates `decision`
    explicitly so a typo doesn't silently no-op."""
    if decision not in ("accept", "reject"):
        return {"error": f"decision must be 'accept' or 'reject', got {decision!r}"}
    try:
        if decision == "accept":
            p = svc.accept_proposal(db, proposal_id, user)
        else:
            p = svc.reject_proposal(db, proposal_id, user, reason=reason)
        db.commit()
    except ValueError as e:
        db.rollback()
        return {"error": str(e)}
    view = review_mod.get_proposal_view(db, p.id)
    return {
        "decided": True,
        "decision": decision,
        "proposal": view._asdict() if view else None,
    }


# ── tool registry (exported) ────────────────────────────────────────────

SCENARIO_TOOLS: list[Tool] = [
    Tool(
        name="scenarios_list_scenarios",
        description=(
            "List every active scenario in the belief engine with current "
            "probability, rank, constraint count, and weakest-link predicate. "
            "Probabilities derive under an explicit independence assumption "
            "(see /scenarios header pill). First call for 'what does the engine "
            "currently believe about future industry states'."
        ),
        input_schema={
            "type": "object", "properties": {}, "additionalProperties": False,
        },
        handler=_h_list_scenarios,
        requires_role="viewer",
    ),
    Tool(
        name="scenarios_get_scenario_detail",
        description=(
            "Per-scenario breakdown: each constraining predicate with its "
            "weight, current P(required state), and weighted log-odds "
            "contribution; plus sensitivity-per-1pp for every constrained "
            "predicate (∂P(scenario)/∂P(predicate=required_state))."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "scenario_key": {
                    "type": "string",
                    "description": "Scenario key (e.g. 'a', 'b', 'c').",
                },
            },
            "required": ["scenario_key"],
            "additionalProperties": False,
        },
        handler=_h_get_scenario_detail,
        requires_role="viewer",
    ),
    Tool(
        name="scenarios_list_predicates",
        description=(
            "List every active predicate with current state probabilities, "
            "30-day velocity for the dominant state, evidence count, and "
            "Shannon entropy (0 = certain, 1 = uniformly contested). Mirrors "
            "the /scenarios Predicates tab."
        ),
        input_schema={
            "type": "object", "properties": {}, "additionalProperties": False,
        },
        handler=_h_list_predicates,
        requires_role="viewer",
    ),
    Tool(
        name="scenarios_get_predicate_detail",
        description=(
            "Per-predicate evidence drill-down. Confirmed evidence sorted by "
            "|log-odds contribution| descending — explains 'why this predicate "
            "moved' fully attributable to specific findings. Includes pending "
            "(LLM-proposed, not yet confirmed) and rejected rows so chat can "
            "answer 'what proposals are awaiting human review'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "predicate_key": {
                    "type": "string",
                    "description": "Predicate key (e.g. 'p1', 'p8').",
                },
            },
            "required": ["predicate_key"],
            "additionalProperties": False,
        },
        handler=_h_get_predicate_detail,
        requires_role="viewer",
    ),
    Tool(
        name="scenarios_get_evidence_for_finding",
        description=(
            "All predicate-evidence rows attached to one finding, with live "
            "log-odds contributions. Useful for 'which predicates does this "
            "finding bear on, and how much does each contribute?'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "finding_id": {
                    "type": "integer",
                    "description": "Finding id (matches Finding.id).",
                },
            },
            "required": ["finding_id"],
            "additionalProperties": False,
        },
        handler=_h_get_evidence_for_finding,
        requires_role="viewer",
    ),
    Tool(
        name="scenarios_update_predicate",
        description=(
            "Edit fields and parameters of one existing predicate. All "
            "fields except predicate_key are optional — omit a field to "
            "leave it unchanged. To edit state priors or labels, pass a "
            "`states` list keyed by state_key; only the listed states are "
            "touched. Validates that priors sum to 1.0 across ALL states "
            "(including unchanged ones) and that an active predicate has "
            "≥2 states. Adding or removing states is not supported here. "
            "Use this to retune a predicate after re-reading the spec or "
            "when fresh evidence implies the prior was off."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "predicate_key": {
                    "type": "string",
                    "description": "Predicate key (e.g. 'p1', 'p8').",
                },
                "name": {"type": "string"},
                "statement": {
                    "type": "string",
                    "description": (
                        "Long-form unambiguous statement of the claim. "
                        "Stage-2 LLM mapping prompts hinge on this — keep "
                        "it precise."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": "discovery | evaluation | transaction | control_point | other",
                },
                "active": {"type": "boolean"},
                "decay_half_life_days": {
                    "type": ["integer", "null"],
                    "description": (
                        "Per-predicate decay override in days. Pass null "
                        "to clear and fall back to the global default. "
                        "Omit to leave unchanged."
                    ),
                },
                "states": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "state_key": {"type": "string"},
                            "label": {"type": "string"},
                            "ordinal_position": {"type": "integer"},
                            "prior_probability": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["state_key"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "Per-state edits keyed by state_key. Each item may "
                        "include any of label/ordinal_position/prior_probability "
                        "— missing keys leave that field alone. The full set of "
                        "priors across the predicate must sum to 1.0."
                    ),
                },
            },
            "required": ["predicate_key"],
            "additionalProperties": False,
        },
        handler=_h_update_predicate,
        requires_role="analyst",
        requires_confirmation=True,
        confirmation_summary=lambda i: (
            f"Update predicate {i.get('predicate_key')!r}? "
            + (
                f"{len(i['states'])} state edit(s)"
                if i.get("states") else "field edits only"
            )
            + ". This rewrites authoring data; cached posterior is preserved."
        ),
    ),
    Tool(
        name="scenarios_propose_predicate",
        description=(
            "Persist one LLM-proposed predicate inline. Same provenance "
            "shape as a batch proposer run — lands in the /predicates "
            "review queue with source='llm_proposed' so a reviewer "
            "can Promote or Reject. Use this when conversation surfaces "
            "a new structural claim worth tracking that isn't covered by "
            "the existing roster (call scenarios_list_predicates first if "
            "unsure). Cite at least 2 finding ids that inspired the "
            "proposal — single-finding speculation is rejected."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": (
                        "Lowercase snake_case slug, 3–5 words, ≤32 chars. "
                        "Descriptive of the claim, not the topic. e.g. "
                        "'agentic_apply_dominates_inbound'."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Short human label.",
                },
                "statement": {
                    "type": "string",
                    "description": (
                        "Long-form precise statement framed as a "
                        "structural claim about the market. Must be "
                        "testable — the classifier reads this later "
                        "to map findings to states."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "discovery | evaluation | transaction | "
                        "control_point. Free-form if none fit."
                    ),
                },
                "states": {
                    "type": "array",
                    "minItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {
                            "state_key": {
                                "type": "string",
                                "description": "Lowercase, no spaces. Stable across renames.",
                            },
                            "label": {"type": "string"},
                            "prior_probability": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                        },
                        "required": ["state_key", "label", "prior_probability"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "≥2 mutually-exclusive states. Priors must sum "
                        "to 1.0 ± 0.001."
                    ),
                },
                "source_finding_ids": {
                    "type": "array",
                    "minItems": 2,
                    "items": {"type": "integer"},
                    "description": (
                        "Real Finding ids that inspired this proposal. "
                        "Cite the strongest 2–5; don't dump every "
                        "adjacent one. Single-finding proposals are "
                        "rejected."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence on why these findings together "
                        "warrant a new predicate rather than evidence "
                        "for an existing one."
                    ),
                },
            },
            "required": [
                "key", "name", "statement", "category",
                "states", "source_finding_ids",
            ],
            "additionalProperties": False,
        },
        handler=_h_predicate_propose,
        requires_role="analyst",
        requires_confirmation=True,
        confirmation_summary=lambda i: (
            f"Propose new predicate {i.get('key')!r} "
            f"({len(i.get('states', []))} states, "
            f"{len(i.get('source_finding_ids', []))} cited findings)? "
            "Lands in the /predicates review queue."
        ),
    ),
    Tool(
        name="scenarios_run_predicate_proposer",
        description=(
            "Kick off a batch predicate-proposal Run — the LLM scans "
            "recent findings + the current roster and writes 0–N new "
            "predicates with source='llm_proposed'. Use when the user "
            "asks to 'sweep for new predicates' or 'find what we're "
            "missing'. Returns the run id + a link; the run streams "
            "log lines into /runs/<id>. For one-off inline proposals, "
            "prefer scenarios_propose_predicate."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "finding_window_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 365,
                    "description": "Window of recent findings to scan. Default 14.",
                },
                "finding_limit": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 500,
                    "description": "Cap on findings fed to the LLM. Default 60.",
                },
                "max_proposals": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Cap on persisted proposals per run. Default 5.",
                },
            },
            "additionalProperties": False,
        },
        handler=_h_run_predicate_proposer,
        requires_role="analyst",
        requires_confirmation=True,
        confirmation_summary=lambda i: (
            "Kick off a predicate-proposer run "
            f"(window={i.get('finding_window_days', 14)}d, "
            f"limit={i.get('finding_limit', 60)} findings, "
            f"max_proposals={i.get('max_proposals', 5)})? "
            "Costs roughly $0.05–$0.20 of LLM time."
        ),
    ),
    Tool(
        name="scenarios_update_scenario",
        description=(
            "Edit fields and parameters of one existing scenario. All "
            "fields except scenario_key are optional — omit to leave "
            "unchanged. To edit link weights or required states, pass a "
            "`links` list keyed by predicate_key; only the listed links "
            "are touched. Validates that weights sum to 1.0 across ALL "
            "links (including unchanged ones) and that every "
            "required_state_key references a real state of that "
            "predicate. Adding or removing links is not supported here."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "scenario_key": {
                    "type": "string",
                    "description": "Scenario key (e.g. 'a', 'b', 'c').",
                },
                "name": {"type": "string"},
                "description": {"type": "string"},
                "active": {"type": "boolean"},
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "predicate_key": {"type": "string"},
                            "required_state_key": {"type": "string"},
                            "weight": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["predicate_key"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "Per-link edits keyed by predicate_key. Each item may "
                        "include weight and/or required_state_key — missing "
                        "keys leave that field alone. The full set of weights "
                        "across the scenario must sum to 1.0."
                    ),
                },
            },
            "required": ["scenario_key"],
            "additionalProperties": False,
        },
        handler=_h_update_scenario,
        requires_role="analyst",
        requires_confirmation=True,
        confirmation_summary=lambda i: (
            f"Update scenario {i.get('scenario_key')!r}? "
            + (
                f"{len(i['links'])} link edit(s)"
                if i.get("links") else "field edits only"
            )
            + "."
        ),
    ),
    Tool(
        name="scenarios_run_predicate_review",
        description=(
            "Enqueue an on-demand monthly-style predicate review. With "
            "no args, reviews every active predicate. Pass "
            "`predicate_keys` to scope to a subset. The job runs through "
            "the run queue (one Haiku call per predicate); it doesn't "
            "block this tool call. Use scenarios_list_pending_proposals "
            "afterwards to read the results."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "predicate_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional. Predicate keys (e.g. ['p1','p4']) to "
                        "review. Omit to review every active predicate."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_h_run_predicate_review,
        requires_role="analyst",
        requires_confirmation=True,
        confirmation_summary=lambda i: (
            "Enqueue predicate review (one LLM call per predicate). Scope: "
            + (",".join(i.get("predicate_keys") or []) or "all active predicates")
            + "."
        ),
    ),
    Tool(
        name="scenarios_list_pending_proposals",
        description=(
            "List predicate-review proposals still in `pending` status. "
            "Optionally scope to one predicate via `predicate_key`. "
            "Each row carries kind (refine_statement / rename_state / "
            "reorder_states / split_state / reassign_evidence / retire), "
            "rationale, supporting findings, and the proposed payload."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "predicate_key": {
                    "type": "string",
                    "description": (
                        "Predicate key (e.g. 'p1'). Omit for the global "
                        "pending queue across every predicate."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_h_list_pending_proposals,
        requires_role="viewer",
    ),
    Tool(
        name="scenarios_decide_proposal",
        description=(
            "Accept or reject one pending PredicateProposal. Accept "
            "applies the change via the existing authoring path "
            "(scenarios_update_predicate semantics); reject is "
            "non-mutating beyond the proposal row. `decision` must be "
            "'accept' or 'reject'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "proposal_id": {"type": "integer"},
                "decision": {
                    "type": "string",
                    "enum": ["accept", "reject"],
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason captured on reject.",
                },
            },
            "required": ["proposal_id", "decision"],
            "additionalProperties": False,
        },
        handler=_h_decide_proposal,
        requires_role="analyst",
        requires_confirmation=True,
        confirmation_summary=lambda i: (
            f"{i.get('decision', '?').capitalize()} predicate proposal "
            f"#{i.get('proposal_id')!r}?"
        ),
    ),
]
