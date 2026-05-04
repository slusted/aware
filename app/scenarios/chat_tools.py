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

from ..chat.tools import Tool
from ..models import User
from . import dashboard as dash


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
]
