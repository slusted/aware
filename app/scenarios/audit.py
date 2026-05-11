"""Roster-level MECE audit (docs/scenarios/07-mece-roster-audit.md).

Pipeline:

    run_mece_audit(db, run_id=None)
        build prompt over all active predicates + evidence digests + unattached findings
        -> single LLM call (or stub)
        -> parse JSON
        -> apply server-side gates
        -> create predicate_proposals (kinds: merge_with, split_predicate, narrow_scope)

This module is read-only over predicate_evidence and predicates. It only
writes to predicate_proposals. Authoring (statement edits, merges, splits)
happens later when a user Accepts a proposal — see
app/scenarios/service.py::accept_proposal.

Failure-soft: missing ANTHROPIC_API_KEY → returns AuditRunResult with no
rows written. JSON parse failure / empty response → log + no rows written;
the next month's run tries again.

The audit does **not** emit `new_predicate` proposals. Coverage-gap
detection is the existing `predicate_proposer` job (different time
signature, sees more recent findings). Any `new_predicate` the LLM emits
is dropped at the gate.

Read paths used by the routes / templates:

    pending_global_proposals(db) -> list[ProposalView]
    digest_for_audit_run(db, run_id) -> AuditDigest
    latest_audit_digest(db) -> AuditDigest | None
"""
from __future__ import annotations

import json
import os
import re
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, NamedTuple

from sqlalchemy.orm import Session

from .. import pricing
from ..db import SessionLocal
from ..models import (
    Finding,
    Predicate,
    PredicateEvidence,
    PredicateProposal,
    PredicateState,
    UsageEvent,
)
from ..skills import load_active
from ..usage import current_run_id
from .review import ProposalView, _proposal_to_view  # reused verbatim
from .service import load_setting


SKILL_NAME = "predicate_mece_audit"
DEFAULT_MODEL = "claude-sonnet-4-6"

# Server-side gate defaults. Spec §Configurability lets these be
# overridden via scenario_settings rows.
DEFAULT_UNATTACHED_DAYS = 30
DEFAULT_UNATTACHED_LIMIT = 40
DEFAULT_TOP_FINDINGS_PER_PRED = 5
DEFAULT_MAX_PROPOSALS = 4
DEFAULT_RETIRED_WINDOW_DAYS = 90
DEFAULT_RETIRED_LIMIT = 5
DEFAULT_AUDIT_CRON = "0 7 1 * *"
DEFAULT_MIN_OVERLAP_PCT = 50
DEFAULT_MIN_SPLIT_SOURCE_FINDINGS = 8
DEFAULT_DIGEST_FRESH_DAYS = 7

# Spec 07 emits only these kinds. Anything else from the LLM is dropped.
AUDIT_KINDS = ("merge_with", "split_predicate", "narrow_scope", "no_op")


# ── Result shapes ──────────────────────────────────────────────────────


@dataclass
class AuditRunResult:
    no_op: bool = False
    proposals_created: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    dropped_at_gate: dict[str, int] = field(default_factory=dict)
    rationale_no_op: str | None = None
    error: str | None = None


class AuditDigest(NamedTuple):
    run_id: int
    completed_at: datetime
    no_op: bool
    by_kind: dict[str, int]
    rationale_no_op: str | None


# ── Config readers ─────────────────────────────────────────────────────


def unattached_days(db: Session) -> int:
    return int(load_setting(db, "mece_audit_unattached_days", DEFAULT_UNATTACHED_DAYS))


def unattached_limit(db: Session) -> int:
    return int(load_setting(db, "mece_audit_unattached_limit", DEFAULT_UNATTACHED_LIMIT))


def top_findings_per_predicate(db: Session) -> int:
    return int(load_setting(
        db, "mece_audit_top_findings_per_predicate", DEFAULT_TOP_FINDINGS_PER_PRED,
    ))


def max_proposals(db: Session) -> int:
    return int(load_setting(db, "mece_audit_max_proposals", DEFAULT_MAX_PROPOSALS))


def audit_cron(db: Session) -> str:
    return str(load_setting(db, "mece_audit_cron", DEFAULT_AUDIT_CRON))


def min_overlap_pct(db: Session) -> int:
    return int(load_setting(db, "mece_audit_min_overlap_pct", DEFAULT_MIN_OVERLAP_PCT))


def min_split_source_findings(db: Session) -> int:
    return int(load_setting(
        db, "mece_audit_min_split_source_findings",
        DEFAULT_MIN_SPLIT_SOURCE_FINDINGS,
    ))


# ── Prompt assembly ────────────────────────────────────────────────────


def _evidence_digest_for_predicate(
    db: Session,
    predicate_id: int,
    *,
    top_n: int,
) -> dict:
    """Roster-view digest of one predicate's confirmed evidence. Cheap
    and stable so the prompt cache hits across runs without roster
    changes.

    Returns:
        {
          "total_count": int,
          "by_strength": {weak,moderate,strong: int},
          "by_source_type": {...: int},   # signal_type bucketing
          "top_findings": [{id, date, title, snippet_120}, ...]
        }
    """
    rows = (
        db.query(PredicateEvidence)
        .filter(
            PredicateEvidence.predicate_id == predicate_id,
            PredicateEvidence.confirmed_at.isnot(None),
            PredicateEvidence.classified_by != "user_rejected",
        )
        .order_by(PredicateEvidence.observed_at.desc())
        .all()
    )
    total = len(rows)
    by_strength: Counter[str] = Counter()
    for r in rows:
        by_strength[r.strength_bucket or "unknown"] += 1

    finding_ids = [r.finding_id for r in rows if r.finding_id]
    findings_by_id: dict[int, Finding] = {}
    if finding_ids:
        for f in (
            db.query(Finding)
            .filter(Finding.id.in_(finding_ids))
            .all()
        ):
            findings_by_id[f.id] = f

    by_source_type: Counter[str] = Counter()
    for r in rows:
        f = findings_by_id.get(r.finding_id) if r.finding_id else None
        if f is None:
            continue
        by_source_type[f.signal_type or "other"] += 1

    top: list[dict] = []
    for r in rows[:top_n]:
        f = findings_by_id.get(r.finding_id) if r.finding_id else None
        if f is None:
            continue
        snippet = (f.summary or f.content or "").strip().replace("\n", " ")
        if len(snippet) > 120:
            snippet = snippet[:117] + "…"
        top.append({
            "id": f.id,
            "date": (f.published_at or f.created_at).strftime("%Y-%m-%d"),
            "title": (f.title or "").strip()[:140],
            "snippet": snippet,
        })

    return {
        "total_count": total,
        "by_strength": dict(by_strength),
        "by_source_type": dict(by_source_type),
        "top_findings": top,
    }


def _format_predicate_block(
    pred: Predicate,
    states: list[PredicateState],
    digest: dict,
) -> str:
    state_lines = "\n".join(
        f"  - {s.state_key}=\"{s.label}\" (current_p={s.current_probability:.2f})"
        for s in states
    )
    strength_str = ", ".join(
        f"{k}={v}" for k, v in sorted(digest["by_strength"].items())
    ) or "none"
    source_str = ", ".join(
        f"{k}={v}" for k, v in sorted(digest["by_source_type"].items())
    ) or "none"
    top_lines = []
    for t in digest["top_findings"]:
        top_lines.append(
            f"    - [id={t['id']} · {t['date']}] {t['title']}\n"
            f"      {t['snippet']}"
        )
    top_block = "\n".join(top_lines) or "    (no findings)"
    return (
        f"## {pred.key} — {pred.name} [{pred.category}]\n"
        f"Statement: {pred.statement}\n"
        f"States:\n{state_lines}\n"
        f"Evidence digest: total={digest['total_count']} · "
        f"strength={{{strength_str}}} · sources={{{source_str}}}\n"
        f"Top findings:\n{top_block}"
    )


def _format_unattached(findings: list[Finding]) -> str:
    if not findings:
        return "(no unattached findings in window)"
    lines = []
    for f in findings:
        date = (f.published_at or f.created_at).strftime("%Y-%m-%d")
        title = (f.title or "").strip()[:140]
        snippet = (f.summary or f.content or "").strip().replace("\n", " ")[:400]
        lines.append(
            f"- [id={f.id} · {date} · {f.source or '?'} · "
            f"{f.competitor or 'no-competitor'}] {title}"
        )
        if snippet:
            lines.append(f"    {snippet}")
    return "\n".join(lines)


def _format_retired(rows: list[Predicate]) -> str:
    if not rows:
        return "(no recently retired predicates)"
    lines = []
    for p in rows:
        when = p.updated_at.strftime("%Y-%m-%d") if p.updated_at else "—"
        lines.append(f"- {p.key} · {p.name} · retired ~{when}")
    return "\n".join(lines)


def build_user_prompt(
    predicates_with_digests: list[tuple[Predicate, list[PredicateState], dict]],
    unattached: list[Finding],
    retired: list[Predicate],
) -> str:
    parts: list[str] = []
    parts.append(f"# Active predicates ({len(predicates_with_digests)})")
    for pred, states, digest in predicates_with_digests:
        parts.append(_format_predicate_block(pred, states, digest))
    parts.append("---")
    parts.append(
        f"# Recent unattached findings ({len(unattached)})\n"
        + _format_unattached(unattached)
    )
    parts.append("---")
    parts.append(
        f"# Recently retired predicates ({len(retired)})\n"
        + _format_retired(retired)
    )
    parts.append("---")
    parts.append(
        "Return JSON only. If the roster is healthy, return exactly one "
        "no_op proposal — never an empty list."
    )
    return "\n\n".join(parts)


# ── Output parsing ─────────────────────────────────────────────────────


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _coerce_int_list(v) -> list[int]:
    out: list[int] = []
    if not isinstance(v, list):
        return out
    for x in v:
        if isinstance(x, int):
            out.append(x)
        elif isinstance(x, str) and x.isdigit():
            out.append(int(x))
    return out


def _coerce_int_or_none(v) -> int | None:
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    try:
        return int(float(v))  # tolerates "75.0"
    except (TypeError, ValueError):
        return None


def parse_audit_response(raw: str | dict) -> list[dict]:
    """Return a list of normalised proposal dicts. Accepts dict or raw
    string. Strips obviously-malformed entries; deeper validation happens
    in `_apply_gates` once we know the active predicate set."""
    parsed = raw if isinstance(raw, dict) else (_extract_json(raw) or {})
    raw_props = parsed.get("proposals")
    if not isinstance(raw_props, list):
        return []
    out: list[dict] = []
    for p in raw_props:
        if not isinstance(p, dict):
            continue
        kind = p.get("kind")
        if kind not in AUDIT_KINDS and kind != "new_predicate":
            # Unknown kind — drop. (new_predicate is kept here so the gate
            # can drop it with a specific reason, for diagnostics.)
            continue
        rationale = (p.get("rationale") or "").strip()[:2000]
        if kind == "no_op":
            out.append({"kind": "no_op", "rationale": rationale})
            continue
        if kind == "merge_with":
            out.append({
                "kind": "merge_with",
                "loser_predicate_key": (p.get("loser_predicate_key") or "").strip() or None,
                "winner_predicate_key": (p.get("winner_predicate_key") or "").strip() or None,
                "rationale": rationale,
                "supporting_finding_ids": _coerce_int_list(p.get("supporting_finding_ids")),
                "evidence_overlap_pct": _coerce_int_or_none(p.get("evidence_overlap_pct")),
            })
        elif kind == "split_predicate":
            new_preds = p.get("new_predicates")
            new_preds_clean = new_preds if isinstance(new_preds, list) else []
            remap_raw = p.get("evidence_remap")
            remap_clean: list[dict] = []
            if isinstance(remap_raw, list):
                for r in remap_raw:
                    if not isinstance(r, dict):
                        continue
                    ev_id = r.get("evidence_id")
                    to_idx = r.get("to_new_index")
                    if isinstance(ev_id, int) and isinstance(to_idx, int):
                        remap_clean.append({
                            "evidence_id": ev_id, "to_new_index": to_idx,
                        })
            out.append({
                "kind": "split_predicate",
                "source_predicate_key": (p.get("source_predicate_key") or "").strip() or None,
                "rationale": rationale,
                "new_predicates": new_preds_clean,
                "evidence_remap": remap_clean,
                "retire_source": bool(p.get("retire_source", True)),
            })
        elif kind == "narrow_scope":
            out.append({
                "kind": "narrow_scope",
                "source_predicate_key": (p.get("source_predicate_key") or "").strip() or None,
                "motivating_predicate_key": (p.get("motivating_predicate_key") or "").strip() or None,
                "new_statement": (p.get("new_statement") or "").strip(),
                "rationale": rationale,
                "supporting_finding_ids": _coerce_int_list(p.get("supporting_finding_ids")),
            })
        elif kind == "new_predicate":
            # Kept so the gate can drop it with a diagnostic.
            out.append({"kind": "new_predicate", "rationale": rationale})
    return out


# ── Server-side gates ──────────────────────────────────────────────────


def _apply_gates(
    proposals: list[dict],
    *,
    valid_keys: set[str],
    pred_evidence_counts: dict[str, int],
    pred_current_statements: dict[str, str],
    overlap_min: int,
    split_source_min: int,
    log_drop: Callable[[str, str], None],
) -> list[dict]:
    """Drop proposals that fail their high-bar gate. `log_drop(kind, why)`
    surfaces drops to the run log so the operator can see why."""
    survivors: list[dict] = []
    for p in proposals:
        kind = p["kind"]

        if kind == "no_op":
            survivors.append(p)
            continue

        if kind == "new_predicate":
            log_drop(kind, "new_predicate is not a valid audit output — proposer handles new predicates")
            continue

        if kind == "merge_with":
            loser = p.get("loser_predicate_key")
            winner = p.get("winner_predicate_key")
            overlap = p.get("evidence_overlap_pct") or 0
            supporting = p.get("supporting_finding_ids") or []
            if not (loser and winner):
                log_drop(kind, "missing loser_predicate_key or winner_predicate_key")
                continue
            if loser == winner:
                log_drop(kind, f"loser == winner ({loser})")
                continue
            if loser not in valid_keys or winner not in valid_keys:
                log_drop(kind, f"unknown predicate key (loser={loser!r}, winner={winner!r})")
                continue
            if overlap < overlap_min:
                log_drop(kind, f"evidence_overlap_pct={overlap} < {overlap_min}")
                continue
            if len(supporting) < 3:
                log_drop(kind, f"supporting_finding_ids has {len(supporting)} ids, need ≥3")
                continue
            survivors.append(p)
            continue

        if kind == "split_predicate":
            src = p.get("source_predicate_key")
            new_preds = p.get("new_predicates") or []
            remap = p.get("evidence_remap") or []
            if not src or src not in valid_keys:
                log_drop(kind, f"unknown source_predicate_key {src!r}")
                continue
            src_count = pred_evidence_counts.get(src, 0)
            if src_count < split_source_min:
                log_drop(kind, f"source has {src_count} findings, need ≥{split_source_min}")
                continue
            if len(new_preds) != 2:
                log_drop(kind, f"new_predicates has {len(new_preds)} entries, need exactly 2")
                continue
            # Each side must have ≥2 evidence rows mapped to it.
            counts: Counter[int] = Counter()
            for r in remap:
                counts[r["to_new_index"]] += 1
            if counts.get(0, 0) < 2 or counts.get(1, 0) < 2:
                log_drop(
                    kind,
                    f"evidence_remap thin (idx0={counts.get(0,0)}, idx1={counts.get(1,0)}); need ≥2 each",
                )
                continue
            # ≥50% of source evidence must be remapped — partial splits
            # leave the source in an awkward state.
            if len(remap) < src_count * 0.5:
                log_drop(
                    kind,
                    f"evidence_remap covers {len(remap)} of {src_count} findings (<50%)",
                )
                continue
            survivors.append(p)
            continue

        if kind == "narrow_scope":
            src = p.get("source_predicate_key")
            mot = p.get("motivating_predicate_key")
            new_stmt = p.get("new_statement") or ""
            if not src or src not in valid_keys:
                log_drop(kind, f"unknown source_predicate_key {src!r}")
                continue
            if not mot or mot not in valid_keys:
                log_drop(kind, f"unknown motivating_predicate_key {mot!r}")
                continue
            if mot == src:
                log_drop(kind, f"motivating_predicate_key == source_predicate_key ({src})")
                continue
            if not new_stmt:
                log_drop(kind, "new_statement is empty")
                continue
            if new_stmt.strip() == (pred_current_statements.get(src) or "").strip():
                log_drop(kind, "new_statement is identical to current statement")
                continue
            survivors.append(p)
            continue

        log_drop(kind, "unknown kind reached gate")  # defensive
    return survivors


# ── Persistence ─────────────────────────────────────────────────────────


def _supersede_same_shape(
    db: Session,
    *,
    source_predicate_key: str | None,
    kind: str,
    target_payload_json: str,
    keep_id: int,
    now: datetime,
) -> int:
    """Mark older pending proposals with the same (source_predicate_key,
    kind, payload) as superseded. Avoids the queue accumulating
    near-duplicates over consecutive audit runs."""
    older = (
        db.query(PredicateProposal)
        .filter(
            PredicateProposal.source_predicate_key == source_predicate_key,
            PredicateProposal.kind == kind,
            PredicateProposal.target_payload_json == target_payload_json,
            PredicateProposal.status == "pending",
            PredicateProposal.id != keep_id,
        )
        .all()
    )
    for o in older:
        o.status = "superseded"
        o.decided_at = now
    return len(older)


def _persist_proposal(
    db: Session, p: dict, *, now: datetime,
) -> tuple[int, str, str]:
    """Insert one survived proposal. Returns (proposal_id, kind, source_key_or_'')
    so the caller can build the digest. Caller commits."""
    kind = p["kind"]
    if kind == "merge_with":
        source_key = p["loser_predicate_key"]
        payload = {
            "loser_predicate_key": p["loser_predicate_key"],
            "winner_predicate_key": p["winner_predicate_key"],
            "evidence_overlap_pct": p.get("evidence_overlap_pct"),
        }
        supporting = p.get("supporting_finding_ids") or []
    elif kind == "split_predicate":
        source_key = p["source_predicate_key"]
        payload = {
            "source_predicate_key": p["source_predicate_key"],
            "new_predicates": p["new_predicates"],
            "evidence_remap": p["evidence_remap"],
            "retire_source": p["retire_source"],
        }
        # Supporting set is the union of evidence_ids touched by the remap.
        supporting = [r["evidence_id"] for r in p["evidence_remap"]][:20]
    elif kind == "narrow_scope":
        source_key = p["source_predicate_key"]
        payload = {
            "source_predicate_key": p["source_predicate_key"],
            "motivating_predicate_key": p["motivating_predicate_key"],
            "new_statement": p["new_statement"],
        }
        supporting = p.get("supporting_finding_ids") or []
    else:
        raise ValueError(f"unsupported kind at persist: {kind!r}")

    payload_str = json.dumps(payload, sort_keys=True)
    row = PredicateProposal(
        kind=kind,
        source_predicate_key=source_key,
        target_payload_json=payload_str,
        rationale=p.get("rationale") or "",
        supporting_finding_ids_json=json.dumps(supporting),
        status="pending",
        created_at=now,
        source_review_id=None,  # audit proposals aren't tied to a PredicateReview row
    )
    db.add(row)
    db.flush()
    _supersede_same_shape(
        db,
        source_predicate_key=source_key,
        kind=kind,
        target_payload_json=payload_str,
        keep_id=row.id,
        now=now,
    )
    return row.id, kind, source_key or ""


# ── LLM client (defaults to live; tests inject a stub) ────────────────


_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    _client = anthropic.Anthropic()
    return _client


def get_model() -> str:
    return os.environ.get("MECE_AUDIT_MODEL", DEFAULT_MODEL)


def _record_usage(model: str, resp) -> None:
    try:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        it = getattr(usage, "input_tokens", 0) or 0
        ot = getattr(usage, "output_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cost = pricing.claude_cost(model, it, ot, cr, cw)
        db = SessionLocal()
        try:
            db.add(UsageEvent(
                run_id=current_run_id.get(),
                provider="claude",
                operation="messages.create",
                model=model,
                input_tokens=it,
                output_tokens=ot,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                cost_usd=cost,
                success=True,
                extra={"caller": "predicate_mece_audit"},
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        traceback.print_exc()


def _live_llm_call(system_prompt: str, user_prompt: str) -> str:
    """Returns the raw text of the assistant response, or "" on failure."""
    client = _get_client()
    if client is None:
        return ""
    model = get_model()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception:
        traceback.print_exc()
        return ""
    _record_usage(model, resp)
    try:
        return resp.content[0].text
    except (AttributeError, IndexError):
        return ""


# ── Main pipeline ──────────────────────────────────────────────────────


def run_mece_audit(
    db: Session,
    *,
    run_id: int | None = None,
    now: datetime | None = None,
    llm_call: Callable[[str, str], str] | None = None,
    log: Callable[[str], None] | None = None,
) -> AuditRunResult:
    """One audit pass over the entire active roster. Single LLM call;
    persists 0..N proposals. Returns a result summary suitable for the
    Run row's progress + the /scenarios digest strip.

    `llm_call(system_prompt, user_prompt) -> str` is injectable for tests.
    """
    if now is None:
        now = datetime.utcnow()
    call = llm_call or _live_llm_call
    log_fn = log or (lambda msg: None)

    result = AuditRunResult()

    skill_body = load_active(SKILL_NAME)
    if not skill_body:
        result.error = f"skill {SKILL_NAME!r} not loaded"
        log_fn(result.error)
        return result

    # ── Roster + digests ──────────────────────────────────────────────
    active_preds = (
        db.query(Predicate)
        .filter(Predicate.active.is_(True))
        .order_by(Predicate.id)
        .all()
    )
    if len(active_preds) < 2:
        # Single-predicate roster can't have a MECE violation. Bail
        # cheaply rather than burning an API call.
        result.no_op = True
        result.rationale_no_op = (
            f"Skipped: only {len(active_preds)} active predicate(s); MECE audit needs ≥2."
        )
        log_fn(result.rationale_no_op)
        return result

    valid_keys = {p.key for p in active_preds}
    pred_current_statements = {p.key: p.statement for p in active_preds}
    states_by_pred: dict[int, list[PredicateState]] = {}
    for s in (
        db.query(PredicateState)
        .order_by(PredicateState.predicate_id, PredicateState.ordinal_position)
        .all()
    ):
        states_by_pred.setdefault(s.predicate_id, []).append(s)

    top_n = top_findings_per_predicate(db)
    digests = [
        _evidence_digest_for_predicate(db, p.id, top_n=top_n)
        for p in active_preds
    ]
    pred_evidence_counts = {
        p.key: d["total_count"] for p, d in zip(active_preds, digests)
    }
    predicates_with_digests = [
        (p, states_by_pred.get(p.id, []), d)
        for p, d in zip(active_preds, digests)
    ]

    # ── Unattached findings (last N days, capped) ─────────────────────
    cutoff = now - timedelta(days=unattached_days(db))
    # "Unattached" = no PredicateEvidence row points at this finding.
    # Use a correlated subquery (pass the Query, SQLAlchemy scalarizes it
    # for the IN-clause) so we don't materialize attached ids in Python.
    attached_finding_ids_q = (
        db.query(PredicateEvidence.finding_id)
        .filter(PredicateEvidence.finding_id.isnot(None))
    )
    unattached = (
        db.query(Finding)
        .filter(
            Finding.created_at >= cutoff,
            Finding.id.notin_(attached_finding_ids_q),
        )
        .order_by(Finding.created_at.desc())
        .limit(unattached_limit(db))
        .all()
    )

    # ── Recently retired predicates ───────────────────────────────────
    retired_cutoff = now - timedelta(days=DEFAULT_RETIRED_WINDOW_DAYS)
    retired = (
        db.query(Predicate)
        .filter(
            Predicate.active.is_(False),
            Predicate.updated_at >= retired_cutoff,
        )
        .order_by(Predicate.updated_at.desc())
        .limit(DEFAULT_RETIRED_LIMIT)
        .all()
    )

    # ── LLM call ──────────────────────────────────────────────────────
    user_prompt = build_user_prompt(predicates_with_digests, unattached, retired)
    log_fn(
        f"calling audit with {len(active_preds)} active predicates, "
        f"{len(unattached)} unattached findings, {len(retired)} recently retired"
    )
    raw = call(skill_body, user_prompt)
    if not raw:
        result.error = "LLM returned empty"
        log_fn(result.error)
        return result

    parsed = parse_audit_response(raw)
    if not parsed:
        result.error = "no valid proposals in LLM response"
        log_fn(result.error)
        return result

    # ── Gates ─────────────────────────────────────────────────────────
    drop_counts: Counter[str] = Counter()

    def _log_drop(kind: str, why: str) -> None:
        drop_counts[kind] += 1
        log_fn(f"dropped {kind!r}: {why}")

    survivors = _apply_gates(
        parsed,
        valid_keys=valid_keys,
        pred_evidence_counts=pred_evidence_counts,
        pred_current_statements=pred_current_statements,
        overlap_min=min_overlap_pct(db),
        split_source_min=min_split_source_findings(db),
        log_drop=_log_drop,
    )

    # ── No-op short-circuit ───────────────────────────────────────────
    # If the LLM emitted a no_op (alone or alongside drops), respect it
    # and write nothing. We never persist no_op rows — it's just
    # surfaced via the digest's rationale field.
    no_op_entries = [p for p in survivors if p["kind"] == "no_op"]
    actionable = [p for p in survivors if p["kind"] != "no_op"]
    if no_op_entries and not actionable:
        result.no_op = True
        result.rationale_no_op = no_op_entries[0].get("rationale") or None
        result.dropped_at_gate = dict(drop_counts)
        log_fn(f"audit complete: no_op (rationale: {result.rationale_no_op!r})")
        return result

    # ── Persist actionable proposals (capped) ─────────────────────────
    cap = max_proposals(db)
    if len(actionable) > cap:
        log_fn(f"capping persisted proposals: {len(actionable)} → {cap}")
        actionable = actionable[:cap]

    by_kind: Counter[str] = Counter()
    for p in actionable:
        try:
            _, kind, _src = _persist_proposal(db, p, now=now)
            by_kind[kind] += 1
            result.proposals_created += 1
        except Exception as e:
            log_fn(f"persist failed for {p.get('kind')!r}: {e}")
            traceback.print_exc()
            continue

    if result.proposals_created:
        db.commit()
    else:
        db.rollback()
        result.no_op = True  # gates dropped everything; behave like a clean run
        result.rationale_no_op = "All proposals failed server-side gates; see drop log."

    result.by_kind = dict(by_kind)
    result.dropped_at_gate = dict(drop_counts)
    log_fn(
        f"audit complete: {result.proposals_created} proposals "
        f"({dict(by_kind)}), dropped {dict(drop_counts) or {}}"
    )
    return result


# ── Read-side helpers used by routes / templates / chat tools ──────────


def pending_global_proposals(
    db: Session,
    *,
    kind: str | None = None,
) -> list[ProposalView]:
    """Pending audit proposals across the whole roster, oldest-first.
    `kind` optionally filters to one of the audit kinds; default returns
    all three. Spec 06's per-predicate proposal kinds are not included
    here — the per-predicate page surfaces those."""
    audit_kinds = ("merge_with", "split_predicate", "narrow_scope")
    q = db.query(PredicateProposal).filter(
        PredicateProposal.status == "pending",
    )
    if kind:
        if kind not in audit_kinds:
            return []
        q = q.filter(PredicateProposal.kind == kind)
    else:
        q = q.filter(PredicateProposal.kind.in_(audit_kinds))
    return [
        _proposal_to_view(r)
        for r in q.order_by(PredicateProposal.id.asc()).all()
    ]


def digest_for_audit_run(db: Session, run_id: int) -> AuditDigest | None:
    """Per-run digest for the /scenarios root strip. Audit runs are
    identified by run_id stamped on the Run row — but the audit doesn't
    write a per-run summary row of its own; we infer from the proposals
    that were persisted. Returns None if no audit-kind proposals exist
    for `run_id`.

    NOTE: PredicateProposal currently has no run_id column. For v1 we
    use a heuristic — proposals created within a small window of the
    Run's finished_at. When PredicateProposal gains a run_id column
    (likely in the same migration that adds predicate_audit_log), this
    becomes a one-line `.filter(run_id=run_id)`.
    """
    from ..models import Run  # local import to avoid module-load cycle

    run = db.get(Run, run_id)
    if run is None or run.finished_at is None:
        return None
    window_start = run.finished_at - timedelta(minutes=5)
    window_end = run.finished_at + timedelta(minutes=5)
    rows = (
        db.query(PredicateProposal)
        .filter(
            PredicateProposal.kind.in_(("merge_with", "split_predicate", "narrow_scope")),
            PredicateProposal.created_at >= window_start,
            PredicateProposal.created_at <= window_end,
        )
        .all()
    )
    by_kind = Counter(r.kind for r in rows)
    return AuditDigest(
        run_id=run_id,
        completed_at=run.finished_at,
        no_op=(len(rows) == 0),
        by_kind=dict(by_kind),
        rationale_no_op=None,  # not persisted in v1; the Run log has it
    )


def latest_audit_digest(
    db: Session,
    *,
    fresh_days: int = DEFAULT_DIGEST_FRESH_DAYS,
    now: datetime | None = None,
) -> AuditDigest | None:
    """Most recent audit run's digest, IF the run completed within
    `fresh_days`. Past that window the /scenarios strip auto-collapses."""
    from ..models import Run

    if now is None:
        now = datetime.utcnow()
    run = (
        db.query(Run)
        .filter(Run.kind == "predicate_mece_audit", Run.finished_at.isnot(None))
        .order_by(Run.finished_at.desc())
        .first()
    )
    if run is None:
        return None
    if (now - run.finished_at) > timedelta(days=fresh_days):
        return None
    return digest_for_audit_run(db, run.id)
