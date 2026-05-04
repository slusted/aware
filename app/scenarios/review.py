"""Monthly predicate review (docs/scenarios/06-predicate-review.md).

Pipeline:

    run_predicate_review(db, predicate_keys=None, run_id=None)
        for each active predicate that is review-due:
            build_prompt -> LLM (or stub) -> parse JSON
            apply server-side gates (high bar for change)
            create predicate_proposals + write predicate_reviews row
            stamp predicate_evidence.fitness*

The math layer is untouched. This module only writes new rows + sets the
three review columns on existing rows. Authoring (statement / state
edits, evidence reassignment) happens later when a user Accepts a
proposal — see app/scenarios/service.py::accept_proposal.

Failure-soft: missing ANTHROPIC_API_KEY → returns ReviewRunResult with
no rows written. JSON parse error / unknown predicate / network error
on a single predicate → log + skip; the next month's run tries again.

Read paths used by the routes / templates:

    latest_review_for(db, predicate_key) -> PredicateReviewView | None
    pending_proposals_for(db, predicate_key) -> list[ProposalView]
    digest_for_run(db, run_id) -> ReviewDigest
"""
from __future__ import annotations

import json
import os
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, NamedTuple

from sqlalchemy.orm import Session

from .. import pricing
from ..db import SessionLocal
from ..models import (
    Finding,
    Predicate,
    PredicateEvidence,
    PredicateProposal,
    PredicateReview,
    PredicateState,
    UsageEvent,
)
from ..usage import current_run_id
from .service import load_setting


DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Server-side gate defaults. Spec §Configurability lets these be
# overridden via scenario_settings rows; load_setting falls back to
# these constants when no row exists.
DEFAULT_MISFIT_THRESHOLD = 3
DEFAULT_BETTER_FIT_THRESHOLD = 2
DEFAULT_DISMISS_DAYS = 30
DEFAULT_MAX_EVIDENCE_IN_PROMPT = 40
DEFAULT_REVIEW_CRON = "0 6 1 * *"
DEFAULT_DIGEST_FRESH_DAYS = 7

VALID_FITNESS = ("fits", "awkward", "misfit")
WORDING_KINDS = ("refine_statement", "rename_state", "reorder_states")
STRUCTURAL_KINDS = ("split_state", "retire")
# Schema accepts these but no UI surfaces them this stage.
RESERVED_KINDS = ("merge_with", "new_predicate")


# ── Result shapes ──────────────────────────────────────────────────────


@dataclass
class ReviewRunResult:
    reviewed: int = 0
    clean: int = 0           # decided_no_change=True
    wording: int = 0         # ≥1 wording proposal
    structural: int = 0      # ≥1 structural proposal
    skipped_cooldown: int = 0
    proposals_created: int = 0
    errors: list[str] = field(default_factory=list)


class PredicateReviewView(NamedTuple):
    id: int
    predicate_key: str
    reviewed_at: datetime
    findings_seen_count: int
    decided_no_change: bool
    summary_text: str
    suggested_actions: list[dict]
    proposal_ids: list[int]


class ProposalView(NamedTuple):
    id: int
    kind: str
    source_predicate_key: str | None
    target_payload: dict
    rationale: str
    supporting_finding_ids: list[int]
    status: str
    created_at: datetime
    decided_at: datetime | None
    decision_reason: str | None


class ReviewDigest(NamedTuple):
    run_id: int
    reviewed: int
    clean: int
    wording: int
    structural: int
    errors: int
    completed_at: datetime | None
    # Predicate keys with at least one pending proposal from this run —
    # rendered as "[Open p1]" buttons on the digest strip.
    predicates_with_proposals: list[str]


# ── Config readers ─────────────────────────────────────────────────────


def misfit_threshold(db: Session) -> int:
    return int(load_setting(
        db, "predicate_review_misfit_threshold", DEFAULT_MISFIT_THRESHOLD,
    ))


def better_fit_threshold(db: Session) -> int:
    return int(load_setting(
        db, "predicate_review_better_fit_threshold", DEFAULT_BETTER_FIT_THRESHOLD,
    ))


def dismiss_days(db: Session) -> int:
    return int(load_setting(
        db, "predicate_review_dismiss_days", DEFAULT_DISMISS_DAYS,
    ))


def max_evidence_in_prompt(db: Session) -> int:
    return int(load_setting(
        db, "predicate_review_max_evidence_in_prompt",
        DEFAULT_MAX_EVIDENCE_IN_PROMPT,
    ))


def review_cron(db: Session) -> str:
    return str(load_setting(
        db, "predicate_review_cron", DEFAULT_REVIEW_CRON,
    ))


# ── Prompt assembly ────────────────────────────────────────────────────


_SYSTEM_PROMPT = """You are auditing a market belief model's ontology hygiene.

For each predicate you'll see its statement, its set of states, every confirmed evidence row mapped to it (target state + direction + strength + finding excerpt), and the prior review's summary if one exists.

Your job is to decide whether the predicate is still well-formed in light of the evidence we've actually accumulated. Default to "looks good" — wording or structural changes are escalations.

Return ONLY a JSON object, no code fences, no commentary:

{{
  "summary_text": "1–3 sentence prose read of the predicate's current state.",
  "decided_no_change": true,
  "fitness_per_evidence": [
    {{"evidence_id": <int>,
      "fitness": "fits" | "awkward" | "misfit",
      "read_as": "one short line glossing how this evidence speaks to the predicate",
      "reassign_target_predicate_key": null
     }}
  ],
  "suggested_actions": [
    {{"kind": "refine_statement" | "rename_state" | "reorder_states" | "split_state" | "retire",
      "rationale": "why",
      "payload": {{...}},
      "supporting_finding_ids": [<int>, ...]
     }}
  ]
}}

Action rules:
- refine_statement payload: {{"new_statement": "..."}}. Use when the wording no longer captures what the evidence is actually about. Needs ≥1 supporting finding.
- rename_state payload: {{"state_key": "<existing_key>", "new_label": "<new label>"}}. Needs ≥1 supporting finding.
- reorder_states payload: {{"order": ["state_key_1", "state_key_2", ...]}}. Must list every existing state_key. Needs ≥1 supporting finding.
- split_state payload: {{"state_key": "<existing_key>", "new_states": [{{"state_key": "<new_key>", "label": "...", "prior": <0–1>}}, ...], "evidence_remap": [{{"evidence_id": <int>, "to_state_key": "<new_key>"}}]}}. Needs ≥3 misfit findings.
- retire payload: {{"reason_short": "..."}}. Needs ≥3 misfit findings.

For evidence that fits a different predicate better, set reassign_target_predicate_key to that predicate's key in the fitness_per_evidence row. Do NOT also include a separate suggested_actions entry — reassign proposals are minted from the evidence row itself.

Set decided_no_change = false if you flagged any evidence as awkward/misfit OR proposed any action.

Keep summary_text concise; quote no more than ~50 chars from any single finding.
"""


def _format_evidence_for_prompt(rows: list[dict]) -> str:
    """One line per evidence row, truncated. Stable order so the prompt
    cache hits across consecutive runs of the same predicate."""
    out = []
    for r in rows:
        snippet = (r.get("excerpt") or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:237] + "…"
        out.append(
            f"- evidence_id={r['evidence_id']} state={r['target_state_key']} "
            f"dir={r['direction']} strength={r['strength_bucket']} "
            f"observed={r['observed_at']} finding="
            f"\"{r['finding_title']}\" :: {snippet}"
        )
    return "\n".join(out) or "(no confirmed evidence)"


def _format_predicate_for_prompt(pred: Predicate, states: list[PredicateState]) -> str:
    state_lines = "\n".join(
        f"  - {s.state_key} (\"{s.label}\", prior={s.prior_probability:.2f})"
        for s in states
    )
    return (
        f"Predicate {pred.key} ({pred.category})\n"
        f"Name: {pred.name}\n"
        f"Statement: {pred.statement}\n"
        f"States:\n{state_lines}"
    )


def build_user_prompt(
    pred: Predicate,
    states: list[PredicateState],
    evidence_rows: list[dict],
    *,
    prior_summary: str | None,
    sampled_count: int | None,
) -> str:
    body = _format_predicate_for_prompt(pred, states)
    body += "\n\nConfirmed evidence:\n" + _format_evidence_for_prompt(evidence_rows)
    if sampled_count is not None and sampled_count > len(evidence_rows):
        body += f"\n\nNote: this predicate has {sampled_count} confirmed findings; the {len(evidence_rows)} most recent are shown."
    if prior_summary:
        body += f"\n\nPrevious review's summary:\n{prior_summary}"
    return body


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


def parse_review_response(raw: str | dict) -> dict:
    """Return a normalised dict. Accepts either a dict (test stub returns
    Python directly) or a string of JSON text (live LLM output)."""
    if isinstance(raw, dict):
        parsed = raw
    else:
        parsed = _extract_json(raw) or {}
    fitness_in = parsed.get("fitness_per_evidence") or []
    actions_in = parsed.get("suggested_actions") or []
    fitness_out = []
    for item in fitness_in:
        if not isinstance(item, dict):
            continue
        ev_id = item.get("evidence_id")
        f = item.get("fitness")
        if not isinstance(ev_id, int) or f not in VALID_FITNESS:
            continue
        fitness_out.append({
            "evidence_id": ev_id,
            "fitness": f,
            "read_as": (item.get("read_as") or "").strip()[:300],
            "reassign_target_predicate_key": (
                item.get("reassign_target_predicate_key") or None
            ),
        })
    actions_out = []
    for a in actions_in:
        if not isinstance(a, dict):
            continue
        kind = a.get("kind")
        if kind not in WORDING_KINDS + STRUCTURAL_KINDS:
            continue
        actions_out.append({
            "kind": kind,
            "rationale": (a.get("rationale") or "").strip()[:1000],
            "payload": a.get("payload") if isinstance(a.get("payload"), dict) else {},
            "supporting_finding_ids": [
                int(x) for x in (a.get("supporting_finding_ids") or [])
                if isinstance(x, int) or (isinstance(x, str) and x.isdigit())
            ],
        })
    return {
        "summary_text": (parsed.get("summary_text") or "").strip(),
        "decided_no_change": bool(parsed.get("decided_no_change", False)),
        "fitness_per_evidence": fitness_out,
        "suggested_actions": actions_out,
    }


# ── LLM call (defaults to live; tests inject a stub) ───────────────────


_client = None


def _get_client():
    """Lazy singleton. Same pattern as classifier._get_client."""
    global _client
    if _client is not None:
        return _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    _client = anthropic.Anthropic()
    return _client


def get_model() -> str:
    return os.environ.get("PREDICATE_REVIEW_MODEL", DEFAULT_MODEL)


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
                extra={"caller": "predicate_review"},
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        traceback.print_exc()


def _live_llm_call(system_prompt: str, user_prompt: str) -> str:
    """Returns the raw text of the assistant response, or "" on failure.
    Caller turns "" into "skip this predicate"."""
    client = _get_client()
    if client is None:
        return ""
    model = get_model()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1500,
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


# ── Server-side gates ──────────────────────────────────────────────────


def _apply_gates(
    parsed: dict,
    *,
    misfit_threshold_n: int,
    valid_predicate_keys: set[str],
) -> list[dict]:
    """Drop any suggested_action that fails its high-bar gate. Returns
    the surviving list. Reassigns aren't represented in suggested_actions
    at all — they're 1:1 with fitness rows whose
    `reassign_target_predicate_key` is set."""
    misfit_ids = {
        f["evidence_id"]
        for f in parsed.get("fitness_per_evidence", [])
        if f["fitness"] == "misfit"
    }

    survivors: list[dict] = []
    for a in parsed.get("suggested_actions", []):
        kind = a["kind"]
        supporting = a.get("supporting_finding_ids") or []
        if kind in WORDING_KINDS:
            if not supporting:
                continue
        elif kind in STRUCTURAL_KINDS:
            # structural needs ≥N supporting findings flagged misfit.
            misfit_supports = [
                fid for fid in supporting if fid in misfit_ids
            ]
            if len(misfit_supports) < misfit_threshold_n:
                continue
        else:
            # Unknown kind — defensive; parse already filters but keep
            # this branch so future kind additions go through gating.
            continue
        survivors.append(a)
    return survivors


# ── Helpers: data loaders for the pipeline ─────────────────────────────


def _load_evidence_for_predicate(
    db: Session,
    predicate_id: int,
    *,
    cap: int,
) -> tuple[list[dict], int]:
    """Confirmed-and-not-rejected rows for one predicate, newest first,
    capped at `cap`. Each row is enriched with the linked finding's
    title + a short excerpt so the prompt has something to chew on.

    Returns (rows, total_count) so the caller can tell the LLM "this is
    a sample of N out of M".
    """
    base = (
        db.query(PredicateEvidence)
        .filter(
            PredicateEvidence.predicate_id == predicate_id,
            PredicateEvidence.confirmed_at.isnot(None),
            PredicateEvidence.classified_by != "user_rejected",
        )
    )
    total = base.count()
    rows = (
        base.order_by(PredicateEvidence.observed_at.desc())
        .limit(cap)
        .all()
    )
    finding_ids = [r.finding_id for r in rows if r.finding_id]
    finding_by_id: dict[int, Finding] = {}
    if finding_ids:
        for f in db.query(Finding).filter(Finding.id.in_(finding_ids)).all():
            finding_by_id[f.id] = f
    out: list[dict] = []
    for r in rows:
        f = finding_by_id.get(r.finding_id) if r.finding_id else None
        excerpt = ""
        if f is not None:
            excerpt = (f.summary or f.content or "").strip()
        out.append({
            "evidence_id": r.id,
            "target_state_key": r.target_state_key,
            "direction": r.direction,
            "strength_bucket": r.strength_bucket,
            "observed_at": r.observed_at.strftime("%Y-%m-%d") if r.observed_at else "",
            "finding_id": r.finding_id,
            "finding_title": (f.title if f else None) or f"#{r.finding_id or 'manual'}",
            "excerpt": excerpt,
        })
    return out, total


def _prior_summary_text(db: Session, predicate_id: int) -> str | None:
    row = (
        db.query(PredicateReview)
        .filter(PredicateReview.predicate_id == predicate_id)
        .order_by(PredicateReview.reviewed_at.desc())
        .first()
    )
    if row is None:
        return None
    return row.summary_text or None


def _supersede_same_shape(
    db: Session,
    *,
    source_predicate_key: str,
    kind: str,
    target_payload_json: str,
    keep_id: int,
) -> int:
    """Mark older pending proposals on the same predicate of the same
    kind + same payload as `superseded`. Returns count superseded."""
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
    now = datetime.utcnow()
    for o in older:
        o.status = "superseded"
        o.decided_at = now
    return len(older)


# ── Main pipeline ──────────────────────────────────────────────────────


def run_predicate_review(
    db: Session,
    *,
    predicate_keys: list[str] | None = None,
    run_id: int | None = None,
    now: datetime | None = None,
    llm_call: Callable[[str, str], str] | None = None,
    log: Callable[[str], None] | None = None,
) -> ReviewRunResult:
    """Walk active predicates and run the review pipeline against each.

    `llm_call(system_prompt, user_prompt) -> str` is injectable so tests
    can plug in a stub that returns canned JSON without an API key. The
    default uses the Anthropic client; if no key is set, the default
    returns "" and we no-op every predicate (no rows written).

    Caller is responsible for the transaction boundary at the end. We
    commit per-predicate so a partial failure leaves the rest behind.
    """
    if now is None:
        now = datetime.utcnow()
    call = llm_call or _live_llm_call
    log_fn = log or (lambda msg: None)

    result = ReviewRunResult()
    cap = max_evidence_in_prompt(db)
    misfit_n = misfit_threshold(db)

    q = db.query(Predicate).filter(Predicate.active.is_(True))
    if predicate_keys:
        q = q.filter(Predicate.key.in_(predicate_keys))
    preds = q.order_by(Predicate.id).all()

    if not preds:
        return result

    # Pull every active predicate key once so reassign targets can be
    # validated without per-row queries.
    valid_keys = {p.key for p in db.query(Predicate).filter(Predicate.active.is_(True)).all()}

    for pred in preds:
        if pred.next_review_due_at and pred.next_review_due_at > now:
            result.skipped_cooldown += 1
            log_fn(f"{pred.key}: skipped (cooldown until {pred.next_review_due_at})")
            continue

        try:
            states = (
                db.query(PredicateState)
                .filter(PredicateState.predicate_id == pred.id)
                .order_by(PredicateState.ordinal_position)
                .all()
            )
            evidence_rows, total = _load_evidence_for_predicate(db, pred.id, cap=cap)
            prior = _prior_summary_text(db, pred.id)

            user_prompt = build_user_prompt(
                pred, states, evidence_rows,
                prior_summary=prior,
                sampled_count=total if total > len(evidence_rows) else None,
            )
            raw = call(_SYSTEM_PROMPT, user_prompt)
            if not raw:
                # No key, network failure, or stub returned empty.
                # Skip without writing a review row — next month tries again.
                log_fn(f"{pred.key}: skipped (LLM returned empty)")
                continue

            parsed = parse_review_response(raw)
            survivors = _apply_gates(
                parsed,
                misfit_threshold_n=misfit_n,
                valid_predicate_keys=valid_keys,
            )

            # ─── Stamp evidence fitness ────────────────────────────────
            ev_by_id = {r["evidence_id"]: r for r in evidence_rows}
            for f in parsed["fitness_per_evidence"]:
                if f["evidence_id"] not in ev_by_id:
                    continue  # LLM hallucinated an id we didn't show it
                ev = db.get(PredicateEvidence, f["evidence_id"])
                if ev is None:
                    continue
                ev.fitness = f["fitness"]
                ev.fitness_read_as = f["read_as"] or None
                ev.fitness_reviewed_at = now

            # ─── Create proposals for surviving suggested_actions ──────
            review_row = PredicateReview(
                predicate_id=pred.id,
                run_id=run_id,
                reviewed_at=now,
                findings_seen_count=total,
                # Filled-in below once we know the proposal count.
                decided_no_change=True,
                summary_text=parsed["summary_text"],
                suggested_actions_json="[]",
                proposal_ids_json="[]",
            )
            db.add(review_row)
            db.flush()  # need review_row.id for source_review_id FK

            created_ids: list[int] = []
            chips: list[dict] = []
            wording_count = 0
            structural_count = 0

            for a in survivors:
                payload_str = json.dumps(a["payload"], sort_keys=True)
                proposal = PredicateProposal(
                    kind=a["kind"],
                    source_predicate_key=pred.key,
                    target_payload_json=payload_str,
                    rationale=a["rationale"],
                    supporting_finding_ids_json=json.dumps(a["supporting_finding_ids"]),
                    status="pending",
                    created_at=now,
                    source_review_id=review_row.id,
                )
                db.add(proposal)
                db.flush()
                created_ids.append(proposal.id)
                _supersede_same_shape(
                    db,
                    source_predicate_key=pred.key,
                    kind=a["kind"],
                    target_payload_json=payload_str,
                    keep_id=proposal.id,
                )
                if a["kind"] in WORDING_KINDS:
                    wording_count += 1
                else:
                    structural_count += 1
                chips.append({
                    "kind": a["kind"],
                    "label": _chip_label(a["kind"], a["payload"]),
                    "proposal_id": proposal.id,
                })

            # ─── Reassign proposals (1:1 with flagged fitness rows) ────
            reassign_count = 0
            for f in parsed["fitness_per_evidence"]:
                target_key = f.get("reassign_target_predicate_key")
                if not target_key or target_key not in valid_keys:
                    continue
                ev = ev_by_id.get(f["evidence_id"])
                if not ev:
                    continue
                payload = {
                    "evidence_id": f["evidence_id"],
                    "to_predicate_key": target_key,
                }
                payload_str = json.dumps(payload, sort_keys=True)
                proposal = PredicateProposal(
                    kind="reassign_evidence",
                    source_predicate_key=pred.key,
                    target_payload_json=payload_str,
                    rationale=f.get("read_as") or "Better fit for another predicate.",
                    supporting_finding_ids_json=json.dumps(
                        [ev["finding_id"]] if ev.get("finding_id") else []
                    ),
                    status="pending",
                    created_at=now,
                    source_review_id=review_row.id,
                )
                db.add(proposal)
                db.flush()
                created_ids.append(proposal.id)
                _supersede_same_shape(
                    db,
                    source_predicate_key=pred.key,
                    kind="reassign_evidence",
                    target_payload_json=payload_str,
                    keep_id=proposal.id,
                )
                reassign_count += 1

            # ─── Finalise the review row ───────────────────────────────
            any_flagged = any(
                f["fitness"] in ("awkward", "misfit")
                for f in parsed["fitness_per_evidence"]
            )
            decided_no_change = (
                len(created_ids) == 0 and not any_flagged
            )
            review_row.decided_no_change = decided_no_change
            # If the LLM wanted "looks good — dismiss for Nd", surface
            # it as a chip too. We don't auto-create a proposal for it
            # — accept happens via the dedicated dismiss button.
            if decided_no_change:
                chips.append({
                    "kind": "looks_good",
                    "label": f"Looks good — dismiss for {dismiss_days(db)}d",
                })
            review_row.suggested_actions_json = json.dumps(chips)
            review_row.proposal_ids_json = json.dumps(created_ids)

            db.commit()

            result.reviewed += 1
            result.proposals_created += len(created_ids)
            if decided_no_change:
                result.clean += 1
            else:
                if structural_count > 0:
                    result.structural += 1
                elif wording_count > 0 or reassign_count > 0:
                    result.wording += 1

            log_fn(
                f"{pred.key}: reviewed "
                f"(findings={total}, proposals={len(created_ids)}, "
                f"clean={decided_no_change})"
            )

        except Exception as e:
            db.rollback()
            tb = traceback.format_exc()
            msg = f"{pred.key}: {e}\n{tb}"
            result.errors.append(msg)
            log_fn(msg)
            continue

    return result


def _chip_label(kind: str, payload: dict) -> str:
    """Short display label for the chip strip on the review block."""
    if kind == "refine_statement":
        return "Refine statement"
    if kind == "rename_state":
        sk = payload.get("state_key", "?")
        return f"Rename state ({sk})"
    if kind == "reorder_states":
        return "Reorder states"
    if kind == "split_state":
        sk = payload.get("state_key", "?")
        return f"Split state ({sk})"
    if kind == "retire":
        return "Retire predicate"
    return kind


# ── Read-side helpers used by routes / templates / chat tools ──────────


def latest_review_for(db: Session, predicate_key: str) -> PredicateReviewView | None:
    pred = (
        db.query(Predicate).filter(Predicate.key == predicate_key).one_or_none()
    )
    if pred is None:
        return None
    row = (
        db.query(PredicateReview)
        .filter(PredicateReview.predicate_id == pred.id)
        .order_by(PredicateReview.reviewed_at.desc())
        .first()
    )
    if row is None:
        return None
    try:
        actions = json.loads(row.suggested_actions_json or "[]")
    except json.JSONDecodeError:
        actions = []
    try:
        proposal_ids = json.loads(row.proposal_ids_json or "[]")
    except json.JSONDecodeError:
        proposal_ids = []
    return PredicateReviewView(
        id=row.id,
        predicate_key=pred.key,
        reviewed_at=row.reviewed_at,
        findings_seen_count=row.findings_seen_count,
        decided_no_change=row.decided_no_change,
        summary_text=row.summary_text,
        suggested_actions=actions,
        proposal_ids=proposal_ids,
    )


def _proposal_to_view(row: PredicateProposal) -> ProposalView:
    try:
        payload = json.loads(row.target_payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    try:
        supporting = json.loads(row.supporting_finding_ids_json or "[]")
    except json.JSONDecodeError:
        supporting = []
    return ProposalView(
        id=row.id,
        kind=row.kind,
        source_predicate_key=row.source_predicate_key,
        target_payload=payload,
        rationale=row.rationale,
        supporting_finding_ids=supporting,
        status=row.status,
        created_at=row.created_at,
        decided_at=row.decided_at,
        decision_reason=row.decision_reason,
    )


def pending_proposals_for(
    db: Session, predicate_key: str | None = None,
) -> list[ProposalView]:
    """Pending proposals for one predicate, or every predicate if
    `predicate_key` is None. Sorted oldest-first so the chat agent can
    walk them in creation order."""
    q = db.query(PredicateProposal).filter(PredicateProposal.status == "pending")
    if predicate_key:
        q = q.filter(PredicateProposal.source_predicate_key == predicate_key)
    return [_proposal_to_view(r) for r in q.order_by(PredicateProposal.id.asc()).all()]


def get_proposal_view(db: Session, proposal_id: int) -> ProposalView | None:
    row = db.get(PredicateProposal, proposal_id)
    return _proposal_to_view(row) if row else None


def digest_for_run(db: Session, run_id: int) -> ReviewDigest | None:
    """Counts for the /scenarios root digest strip. Returns None if no
    review rows exist for this run."""
    rows = (
        db.query(PredicateReview)
        .filter(PredicateReview.run_id == run_id)
        .all()
    )
    if not rows:
        return None
    reviewed = len(rows)
    clean = sum(1 for r in rows if r.decided_no_change)
    wording = 0
    structural = 0
    pred_ids_with_proposals: set[int] = set()
    for r in rows:
        if r.decided_no_change:
            continue
        try:
            ids = json.loads(r.proposal_ids_json or "[]")
        except json.JSONDecodeError:
            ids = []
        if not ids:
            continue
        pred_ids_with_proposals.add(r.predicate_id)
        kinds = {
            (db.get(PredicateProposal, pid) or PredicateProposal()).kind
            for pid in ids
        }
        if kinds & set(STRUCTURAL_KINDS):
            structural += 1
        elif kinds & set(WORDING_KINDS) or "reassign_evidence" in kinds:
            wording += 1
    pred_keys = []
    if pred_ids_with_proposals:
        for p in (
            db.query(Predicate)
            .filter(Predicate.id.in_(pred_ids_with_proposals))
            .order_by(Predicate.id)
            .all()
        ):
            pred_keys.append(p.key)
    completed_at = max(r.reviewed_at for r in rows)
    return ReviewDigest(
        run_id=run_id,
        reviewed=reviewed,
        clean=clean,
        wording=wording,
        structural=structural,
        # Not currently surfaced separately — the caller can query the
        # Run.error if any. Reserved 0 here so the strip's "0 errors"
        # cell renders.
        errors=0,
        completed_at=completed_at,
        predicates_with_proposals=pred_keys,
    )


def latest_digest(
    db: Session, *, fresh_days: int = DEFAULT_DIGEST_FRESH_DAYS, now: datetime | None = None,
) -> ReviewDigest | None:
    """The most recent run's digest, IF the run completed within
    `fresh_days`. Used by the /scenarios root strip — past the freshness
    window the strip auto-collapses (returns None)."""
    from datetime import timedelta

    if now is None:
        now = datetime.utcnow()
    latest = (
        db.query(PredicateReview)
        .order_by(PredicateReview.reviewed_at.desc())
        .first()
    )
    if latest is None or latest.run_id is None:
        return None
    if (now - latest.reviewed_at) > timedelta(days=fresh_days):
        return None
    return digest_for_run(db, latest.run_id)
