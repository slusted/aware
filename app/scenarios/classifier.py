"""LLM classifier: maps a Finding to 0–2 PredicateEvidence proposals.

One Haiku call per finding. The system prompt holds the predicate roster
and is marked for prompt caching, so a back-to-back sweep over N
findings sends the roster once and hits the cache for the remaining
N-1 calls. Per-finding cost lands around $0.001 with cache hits.

Failure-soft: missing ANTHROPIC_API_KEY → returns []. JSON parse error,
unknown predicate, invalid state, network error → returns []. The
sweep stamps `findings.scenarios_classified_at` either way so we don't
retry the same finding forever.

Output of every successful call is a list of ProposedEvidence (capped
at 2 — the PRD's per-finding limit). The sweep layer turns those into
predicate_evidence rows with classified_by="llm" and confirmed_at=NULL.

Cost auditing: each call inserts a UsageEvent with
extra={"caller": "scenarios_classifier"} so the daily-budget guard in
sweep.py can sum spend without confusing this classifier with the
unrelated llm_classify pass that uses the same Haiku model.

See docs/scenarios/02-card-tagging.md §"Classifier".
"""
from __future__ import annotations

import json
import os
import re
import traceback
from typing import NamedTuple

import anthropic

from ..db import SessionLocal
from ..models import UsageEvent
from .. import pricing
from ..usage import current_run_id


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_EVIDENCE_PER_FINDING = 2

_VALID_DIRECTIONS = ("support", "contradict", "neutral")
_VALID_STRENGTHS = ("weak", "moderate", "strong")
_MAX_FINDING_CONTENT_CHARS = 2000


class PredicateRoster(NamedTuple):
    """Snapshot of predicates + their valid states, fed into the prompt
    and used for output validation. Built once per sweep."""
    predicates: list[dict]
    # {predicate_key: {state_key, ...}} for fast validation
    valid_states: dict[str, set[str]]


class ProposedEvidence(NamedTuple):
    predicate_key: str
    target_state_key: str
    direction: str
    strength_bucket: str
    confidence: float
    reasoning: str


# ── Roster construction ─────────────────────────────────────────────────

def build_roster(predicates_with_states: list[dict]) -> PredicateRoster:
    """Build a PredicateRoster from the service-layer query result.

    Input shape (one entry per active predicate):
      {"key": "p1", "name": "...", "statement": "...", "category": "...",
       "states": [{"state_key": "platform", "label": "Platform-dominant"}, ...]}
    """
    valid_states: dict[str, set[str]] = {}
    for p in predicates_with_states:
        valid_states[p["key"]] = {s["state_key"] for s in p.get("states", [])}
    return PredicateRoster(
        predicates=predicates_with_states,
        valid_states=valid_states,
    )


def _format_roster_for_prompt(roster: PredicateRoster) -> str:
    """Render the roster as the markdown block the system prompt embeds."""
    lines: list[str] = []
    for p in roster.predicates:
        states = " | ".join(
            f"{s['state_key']}={s['label']}" for s in p.get("states", [])
        )
        lines.append(
            f"- {p['key']} ({p['category']}): {p['statement']}\n"
            f"  States: {states}"
        )
    return "\n".join(lines)


# ── Prompt assembly ─────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = """You are mapping competitive-intelligence findings to predicates in a market belief model for {company} in {industry}.

For each finding you receive, return a JSON object with one field: `evidence`.
`evidence` is an array of 0–{max_per_finding} objects. 0 = the finding doesn't bear on any predicate. 1 = the finding meaningfully moves one predicate. 2 = the finding moves two predicates (rare; only when both are clearly affected by the same underlying observation, not when one is a downstream consequence).

Each evidence object has these fields:
- predicate_key: one of {predicate_keys}
- target_state_key: must be a valid state for that predicate (see roster below)
- direction: "support" | "contradict" | "neutral"
- strength_bucket: "weak" | "moderate" | "strong"
- confidence: 0.0-1.0 — how confident you are in this mapping
- reasoning: one short sentence explaining the link

Strength bucket guide:
- strong: a definitive, public, executed move (a launch, an acquisition, an earnings call statement) that directly demonstrates the state.
- moderate: a credible signal short of execution (a named hire suggesting a direction, a partnership announcement, a public roadmap commitment).
- weak: an indirect signal (a job posting, a blog post, a passing exec mention).

Use direction="neutral" when the finding mentions a predicate but doesn't clearly support or contradict any one state — it's noted but doesn't move the math.

Predicate roster:
{roster}

Return ONLY the JSON object. No code fences. No commentary.

Example output for a finding that supports two predicates:
{{"evidence": [{{"predicate_key": "p1", "target_state_key": "agent", "direction": "support", "strength_bucket": "strong", "confidence": 0.85, "reasoning": "Concrete agent-driven distribution at scale."}}, {{"predicate_key": "p4", "target_state_key": "model_led", "direction": "support", "strength_bucket": "moderate", "confidence": 0.7, "reasoning": "Implies model-driven matching as the routing mechanism."}}]}}

Example output for a finding that bears on nothing:
{{"evidence": []}}
"""


def build_system_prompt(
    roster: PredicateRoster,
    *,
    company: str = "our company",
    industry: str = "our industry",
) -> str:
    """Assemble the cacheable system prompt. Stable across all findings in
    a single sweep so the cache hit rate is high."""
    return _SYSTEM_TEMPLATE.format(
        company=company,
        industry=industry,
        max_per_finding=MAX_EVIDENCE_PER_FINDING,
        predicate_keys=sorted(roster.valid_states.keys()),
        roster=_format_roster_for_prompt(roster),
    )


def _user_prompt(finding: dict) -> str:
    """Per-finding payload. Kept short and structured — Haiku does
    better with the metadata fields broken out than with one prose blob."""
    title = (finding.get("title") or "").strip()
    summary = (finding.get("summary") or "").strip()
    content = (finding.get("content") or "").strip()
    body = summary or content[:_MAX_FINDING_CONTENT_CHARS]
    competitor = finding.get("competitor") or "unknown"
    source = finding.get("source") or "unknown"
    signal_type = finding.get("signal_type") or "unclassified"
    published = finding.get("published_at") or finding.get("created_at")
    pub_str = (
        published.strftime("%Y-%m-%d") if hasattr(published, "strftime") else str(published or "")
    )

    return (
        f"Competitor: {competitor}\n"
        f"Source: {source}  Signal type: {signal_type}\n"
        f"Published: {pub_str}\n"
        f"Title: {title}\n\n"
        f"Content:\n{body}"
    )


# ── Output parsing ──────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Pull the first {...} block out of the response and json.loads it.
    Tolerates code fences and surrounding prose."""
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


def parse_response(raw: str, roster: PredicateRoster) -> list[ProposedEvidence]:
    """Validate and normalize the LLM response. Drops any entry that
    references an unknown predicate or invalid state. Caps at
    MAX_EVIDENCE_PER_FINDING."""
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        return []
    raw_evidence = parsed.get("evidence")
    if not isinstance(raw_evidence, list):
        return []

    out: list[ProposedEvidence] = []
    for item in raw_evidence[:MAX_EVIDENCE_PER_FINDING]:
        if not isinstance(item, dict):
            continue
        pkey = item.get("predicate_key")
        skey = item.get("target_state_key")
        direction = item.get("direction")
        strength = item.get("strength_bucket")
        if pkey not in roster.valid_states:
            continue
        if skey not in roster.valid_states[pkey]:
            continue
        if direction not in _VALID_DIRECTIONS:
            continue
        if strength not in _VALID_STRENGTHS:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        reasoning = (item.get("reasoning") or "").strip()
        if len(reasoning) > 500:
            reasoning = reasoning[:497] + "…"
        out.append(ProposedEvidence(
            predicate_key=pkey,
            target_state_key=skey,
            direction=direction,
            strength_bucket=strength,
            confidence=confidence,
            reasoning=reasoning,
        ))
    return out


# ── API call ────────────────────────────────────────────────────────────

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic | None:
    """Lazy singleton. Mirrors signals/llm_classify.py so a key set via
    /settings/keys takes effect on the next call (the env_keys refresh
    nulls _client there; we follow the same convention so the same
    refresh hook can null us by name later)."""
    global _client
    if _client is not None:
        return _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    _client = anthropic.Anthropic()
    return _client


def get_model() -> str:
    """Configurable via env so a future Haiku rev can be swapped without
    a code change."""
    return os.environ.get("SCENARIOS_CLASSIFIER_MODEL", DEFAULT_MODEL)


def _record_usage(model: str, resp) -> None:
    """Same shape as usage.record_claude but stamps extra.caller so the
    sweep's daily-budget guard can sum spend specific to this classifier
    and not confuse it with the unrelated Haiku usage in
    signals/llm_classify.py."""
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
                extra={"caller": "scenarios_classifier"},
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        traceback.print_exc()


def classify_finding(
    finding: dict,
    roster: PredicateRoster,
    *,
    system_prompt: str | None = None,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
) -> list[ProposedEvidence]:
    """Single Haiku call. Returns a (possibly empty) list of
    ProposedEvidence. Returns [] on any failure — callers must check the
    return length, not assume errors raise.

    `system_prompt` and `client` are injectable so the sweep can build
    the system once per batch and so tests can stub the API. Both
    default to the production path.
    """
    if client is None:
        client = _get_client()
    if client is None:
        return []
    if model is None:
        model = get_model()
    if system_prompt is None:
        # Caller didn't pre-build; do it here. Slower path (no cache
        # sharing across calls) but supported for one-off classification.
        system_prompt = build_system_prompt(roster)

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": _user_prompt(finding),
            }],
        )
    except Exception:
        traceback.print_exc()
        return []

    _record_usage(model, resp)

    try:
        raw = resp.content[0].text
    except (AttributeError, IndexError):
        return []
    return parse_response(raw, roster)
