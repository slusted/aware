"""LLM-driven predicate proposer (Phase 3b).

Where the classifier maps an incoming finding onto an existing predicate's
states, this module asks: *given a slice of recent findings plus the
current roster, are there any new predicates worth adding that the
roster doesn't already cover?*

Output is written into the `predicates` table with `source='llm_proposed'`
and a `proposal_metadata` JSON blob (finding_ids, reason, model,
proposed_at). Reviewers act on these via /predicates?source=llm_proposed
(Phase 3a UI). The proposer never touches user-authored predicates.

Single entry point: `propose_predicates(db, ...)` returning a
ProposerResult. Wired into the Run dispatcher under kind
'predicate_proposal' (see app/jobs.py).
"""
from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..models import Finding, Predicate, PredicateState
from ..skills import load_active


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_FINDING_WINDOW_DAYS = 14
DEFAULT_FINDING_LIMIT = 60
DEFAULT_MAX_PROPOSALS = 5
KEY_MAX_LEN = 32
PROPOSER_SKILL_NAME = "predicate_proposer"


# ─── Shapes ─────────────────────────────────────────────────────────────

@dataclass
class ProposedPredicate:
    """A successfully-validated proposal ready for persistence. Mirrors
    the LLM's JSON shape but post-validation, with conflict-resolved
    key. Persistence happens in `_persist`."""
    key: str
    name: str
    statement: str
    category: str
    states: list[dict]                # [{state_key, label, prior_probability}]
    source_finding_ids: list[int]
    reason: str


@dataclass
class ProposerResult:
    findings_considered: int
    proposals_returned: int           # raw LLM proposal count
    proposals_persisted: int          # post-dedupe + validation
    proposals_skipped_duplicate: int
    proposals_skipped_invalid: int
    error: str | None = None


# ─── LLM client (lazy) ──────────────────────────────────────────────────

def _get_client():
    """Reuse the analyzer module's client so usage tracking + key
    handling are centralized. Returns None if the API isn't configured —
    callers must check."""
    try:
        import analyzer
        return analyzer.client
    except Exception:
        return None


# ─── Prompt assembly ────────────────────────────────────────────────────

def _format_existing_roster(preds: list[Predicate],
                            states_by_pred: dict[int, list[PredicateState]]) -> str:
    """Plain-text roster fed to the LLM so it can avoid duplicating an
    existing predicate. Inactive predicates are excluded — they're
    already retired and a proposal that revives one is fine to flag."""
    if not preds:
        return "(no existing predicates yet — propose freely)"
    lines = []
    for p in preds:
        state_labels = [
            f"{s.state_key}={s.label}"
            for s in sorted(states_by_pred.get(p.id, []), key=lambda s: s.ordinal_position)
        ]
        lines.append(
            f"- {p.key}: {p.name} [{p.category}] "
            f"states=[{', '.join(state_labels) or 'none'}]"
        )
        if p.statement:
            # Statement matters for the LLM to gauge orthogonality — what
            # is the predicate actually claiming? Truncate generously.
            lines.append(f"    statement: {p.statement[:300]}")
    return "\n".join(lines)


def _format_findings(findings: list[Finding]) -> str:
    """Compact representation. Each line carries the finding id (the LLM
    cites these in source_finding_ids), the date, source, title, and a
    truncated content snippet. Format mirrors the classifier's so the
    model is on familiar ground."""
    if not findings:
        return "(no findings in window)"
    lines = []
    for f in findings:
        date = f.created_at.strftime("%Y-%m-%d") if f.created_at else "—"
        title = (f.title or "").strip()[:140]
        snippet = (f.content or "").strip().replace("\n", " ")[:400]
        lines.append(
            f"- [id={f.id} · {date} · {f.source or '?'} · "
            f"{f.competitor or 'no-competitor'}] {title}"
        )
        if snippet:
            lines.append(f"    {snippet}")
    return "\n".join(lines)


def _build_prompt(skill_body: str,
                  preds: list[Predicate],
                  states_by_pred: dict[int, list[PredicateState]],
                  findings: list[Finding]) -> tuple[str, str]:
    """Returns (system, user) for the messages.create call."""
    system = skill_body
    user = (
        "# Existing predicate roster\n"
        f"{_format_existing_roster(preds, states_by_pred)}\n\n"
        "---\n"
        f"# Recent findings ({len(findings)})\n"
        f"{_format_findings(findings)}\n\n"
        "---\n"
        "Return JSON only. Empty proposals list is a valid response."
    )
    return system, user


# ─── Response parsing ───────────────────────────────────────────────────

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict | None:
    """The skill instructs the LLM to return raw JSON, but defensively
    we also handle a stray markdown fence or leading prose. Returns
    None if no parseable object can be salvaged."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJ_RE.search(raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _coerce_str(v: Any) -> str | None:
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return None


def _validate_proposal(raw: dict, valid_finding_ids: set[int]) -> tuple[ProposedPredicate | None, str | None]:
    """Return (proposed, None) on success or (None, reason) on rejection.
    Reasons are short and operator-readable — they go into RunEvent
    log lines so a watcher can see why proposals are being dropped."""
    if not isinstance(raw, dict):
        return None, "not an object"

    key = _coerce_str(raw.get("key"))
    name = _coerce_str(raw.get("name"))
    statement = _coerce_str(raw.get("statement"))
    category = _coerce_str(raw.get("category"))
    reason = _coerce_str(raw.get("reason")) or ""

    if not (key and name and statement and category):
        return None, "missing required string field (key/name/statement/category)"

    if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
        return None, f"key {key!r} not snake_case"
    if len(key) > KEY_MAX_LEN:
        return None, f"key {key!r} > {KEY_MAX_LEN} chars"

    states_raw = raw.get("states")
    if not isinstance(states_raw, list) or len(states_raw) < 2:
        return None, "states must be a list of 2+ items"

    parsed_states = []
    seen_keys = set()
    prior_total = 0.0
    for s in states_raw:
        if not isinstance(s, dict):
            return None, "state entry not an object"
        sk = _coerce_str(s.get("state_key"))
        sl = _coerce_str(s.get("label"))
        sp = s.get("prior_probability")
        if not (sk and sl):
            return None, "state missing state_key or label"
        if not re.fullmatch(r"[a-z][a-z0-9_]*", sk):
            return None, f"state_key {sk!r} not snake_case"
        if sk in seen_keys:
            return None, f"duplicate state_key {sk!r}"
        seen_keys.add(sk)
        try:
            sp_f = float(sp)
        except (TypeError, ValueError):
            return None, f"state {sk!r} prior_probability not numeric"
        if not (0.0 <= sp_f <= 1.0):
            return None, f"state {sk!r} prior out of [0,1]"
        prior_total += sp_f
        parsed_states.append({
            "state_key": sk,
            "label": sl,
            "prior_probability": sp_f,
        })
    if abs(prior_total - 1.0) > 0.001:
        return None, f"priors sum to {prior_total:.4f}, not 1.0"

    sfi_raw = raw.get("source_finding_ids")
    if not isinstance(sfi_raw, list) or len(sfi_raw) < 2:
        return None, "source_finding_ids must be a list of 2+"
    finding_ids: list[int] = []
    for v in sfi_raw:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return None, f"non-integer finding id {v!r}"
        if iv not in valid_finding_ids:
            return None, f"finding id {iv} not in batch"
        finding_ids.append(iv)
    # Cap at 5 — we keep provenance lean.
    finding_ids = finding_ids[:5]

    return ProposedPredicate(
        key=key,
        name=name,
        statement=statement,
        category=category,
        states=parsed_states,
        source_finding_ids=finding_ids,
        reason=reason,
    ), None


# ─── Dedupe + persist ───────────────────────────────────────────────────

def _name_token_set(s: str) -> set[str]:
    """Lowercase token set for cheap fuzzy-name comparison. Drops
    punctuation. Used only for the duplicate-name guard — strict matching
    happens on key uniqueness."""
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def _is_duplicate(prop: ProposedPredicate, existing: list[Predicate]) -> bool:
    """True if `prop` overlaps an existing predicate enough that
    persisting it would be noise. Three checks:
      1. Exact key collision (after fallback-suffix the LLM might still
         clash if it picked a real key).
      2. Exact case-folded name match.
      3. Token-overlap >= 80% on names — catches close rephrasings.
    The reviewer can still create a real duplicate manually if they
    really want; the proposer is the one being conservative."""
    prop_tokens = _name_token_set(prop.name)
    if not prop_tokens:
        return False
    for p in existing:
        if p.key == prop.key:
            return True
        if p.name.strip().lower() == prop.name.strip().lower():
            return True
        existing_tokens = _name_token_set(p.name)
        if not existing_tokens:
            continue
        overlap = len(prop_tokens & existing_tokens)
        denom = max(len(prop_tokens), len(existing_tokens))
        if denom and overlap / denom >= 0.8:
            return True
    return False


def _resolve_key(proposed: str, taken: set[str]) -> str:
    """Append _2, _3, … until unique. Belt-and-braces — _is_duplicate
    catches semantic dupes, this catches accidental key clashes only."""
    if proposed not in taken:
        return proposed
    base = proposed[: KEY_MAX_LEN - 3]
    for n in range(2, 100):
        candidate = f"{base}_{n}"
        if candidate not in taken and len(candidate) <= KEY_MAX_LEN:
            return candidate
    # Extremely unlikely; raise so the operator sees it rather than
    # silently skipping the proposal.
    raise ValueError(f"could not resolve unique key for {proposed!r}")


def _persist(db: Session,
             proposal: ProposedPredicate,
             *,
             model: str,
             now: datetime) -> Predicate:
    """Insert one Predicate + its PredicateState rows. Caller commits."""
    p = Predicate(
        key=proposal.key,
        name=proposal.name,
        statement=proposal.statement,
        category=proposal.category,
        active=True,
        source="llm_proposed",
        proposal_metadata={
            "finding_ids": proposal.source_finding_ids,
            "reason": proposal.reason,
            "model": model,
            "proposed_at": now.isoformat(timespec="seconds"),
        },
        created_at=now,
        updated_at=now,
    )
    db.add(p)
    db.flush()  # need p.id for state FKs
    for ord_idx, st in enumerate(proposal.states):
        db.add(PredicateState(
            predicate_id=p.id,
            state_key=st["state_key"],
            label=st["label"],
            ordinal_position=ord_idx,
            prior_probability=st["prior_probability"],
            current_probability=st["prior_probability"],
            updated_at=now,
        ))
    return p


# ─── Entry point ────────────────────────────────────────────────────────

def propose_predicates(
    db: Session,
    *,
    finding_window_days: int = DEFAULT_FINDING_WINDOW_DAYS,
    finding_limit: int = DEFAULT_FINDING_LIMIT,
    max_proposals: int = DEFAULT_MAX_PROPOSALS,
    model: str | None = None,
    now: datetime | None = None,
    log: callable | None = None,
) -> ProposerResult:
    """Run one proposer cycle. Designed to be called from a Run job
    (jobs.run_predicate_proposal_job) but works standalone for testing.

    `log(message, level='info')` is an optional callback that lets the
    Run job stream progress into RunEvent. No log = silent.
    """
    def _log(msg: str, level: str = "info") -> None:
        if log:
            log(msg, level)

    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=finding_window_days)

    findings = (
        db.query(Finding)
        .filter(Finding.created_at >= cutoff)
        .order_by(Finding.created_at.desc())
        .limit(finding_limit)
        .all()
    )
    if not findings:
        _log(f"no findings in last {finding_window_days}d — nothing to propose")
        return ProposerResult(
            findings_considered=0,
            proposals_returned=0,
            proposals_persisted=0,
            proposals_skipped_duplicate=0,
            proposals_skipped_invalid=0,
        )

    valid_finding_ids = {f.id for f in findings}

    # Existing roster — both active and inactive count for dedupe purposes
    # (we don't want to re-propose a predicate the user retired). But the
    # roster shown to the LLM only includes active ones — inactive are
    # by definition "decided against".
    all_existing = db.query(Predicate).order_by(Predicate.id).all()
    active_existing = [p for p in all_existing if p.active]
    states_by_pred: dict[int, list[PredicateState]] = {}
    for s in db.query(PredicateState).all():
        states_by_pred.setdefault(s.predicate_id, []).append(s)

    skill_body = load_active(PROPOSER_SKILL_NAME)
    if not skill_body:
        _log(f"skill {PROPOSER_SKILL_NAME!r} missing — aborting", "error")
        return ProposerResult(
            findings_considered=len(findings),
            proposals_returned=0,
            proposals_persisted=0,
            proposals_skipped_duplicate=0,
            proposals_skipped_invalid=0,
            error=f"skill {PROPOSER_SKILL_NAME!r} not found",
        )

    client = _get_client()
    if client is None:
        _log("anthropic client not configured — aborting", "error")
        return ProposerResult(
            findings_considered=len(findings),
            proposals_returned=0,
            proposals_persisted=0,
            proposals_skipped_duplicate=0,
            proposals_skipped_invalid=0,
            error="anthropic client not configured",
        )

    use_model = model or DEFAULT_MODEL
    system, user = _build_prompt(skill_body, active_existing, states_by_pred, findings)

    _log(
        f"calling {use_model} with {len(findings)} findings + "
        f"{len(active_existing)} active predicates"
    )
    try:
        resp = client.messages.create(
            model=use_model,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        traceback.print_exc()
        _log(f"LLM call failed: {e}", "error")
        return ProposerResult(
            findings_considered=len(findings),
            proposals_returned=0,
            proposals_persisted=0,
            proposals_skipped_duplicate=0,
            proposals_skipped_invalid=0,
            error=f"llm call failed: {e}",
        )

    try:
        raw_text = resp.content[0].text
    except (AttributeError, IndexError):
        _log("LLM returned empty response", "error")
        return ProposerResult(
            findings_considered=len(findings),
            proposals_returned=0,
            proposals_persisted=0,
            proposals_skipped_duplicate=0,
            proposals_skipped_invalid=0,
            error="empty LLM response",
        )

    parsed = _extract_json(raw_text)
    if parsed is None:
        _log("could not parse JSON from LLM response", "error")
        return ProposerResult(
            findings_considered=len(findings),
            proposals_returned=0,
            proposals_persisted=0,
            proposals_skipped_duplicate=0,
            proposals_skipped_invalid=0,
            error="json parse failed",
        )

    raw_proposals = parsed.get("proposals") if isinstance(parsed, dict) else None
    if not isinstance(raw_proposals, list):
        _log("response had no 'proposals' list — treating as zero proposals", "warn")
        raw_proposals = []

    _log(f"LLM returned {len(raw_proposals)} proposals")

    persisted = 0
    skipped_dup = 0
    skipped_inv = 0
    seen_keys = {p.key for p in all_existing}

    for i, raw in enumerate(raw_proposals[:max_proposals]):
        prop, err = _validate_proposal(raw, valid_finding_ids)
        if not prop:
            skipped_inv += 1
            _log(f"proposal #{i} rejected: {err}", "warn")
            continue
        if _is_duplicate(prop, all_existing):
            skipped_dup += 1
            _log(f"proposal {prop.key!r} skipped — overlaps existing predicate", "warn")
            continue
        try:
            prop.key = _resolve_key(prop.key, seen_keys)
        except ValueError as e:
            skipped_inv += 1
            _log(f"proposal {prop.key!r} skipped — {e}", "warn")
            continue
        seen_keys.add(prop.key)
        _persist(db, prop, model=use_model, now=now)
        persisted += 1
        _log(f"proposed {prop.key!r}: {prop.name}", "info")

    if persisted:
        db.commit()
    else:
        db.rollback()  # nothing to keep; release any flushed state

    return ProposerResult(
        findings_considered=len(findings),
        proposals_returned=len(raw_proposals),
        proposals_persisted=persisted,
        proposals_skipped_duplicate=skipped_dup,
        proposals_skipped_invalid=skipped_inv,
    )
