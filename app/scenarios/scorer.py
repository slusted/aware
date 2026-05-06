"""Sonnet-driven multi-pass evidence scorer.

Runs after the Haiku triage classifier ([classifier.py]) has emitted a
ProposedEvidence for a finding. For each proposal, scores four
independent dimensions of evidence quality from the
`predicate_scorer` skill (registered in app/skills.py KNOWN_SKILLS):

  - mechanism (present yes/no, type)
  - base_rate (high/medium/low)
  - counter_evidence (none/weak/strong + example)
  - incentive_bias (+/−/0)

The skill body is loaded through `app.skills.load_active(...)` so it
appears in the admin /settings/skills tab and edits made through the UI
take effect on the very next call (no redeploy). The body is pinned as
the system prompt with ephemeral cache_control, so a back-to-back sweep
over N proposals sends the skill once and hits the prompt cache for
the remaining N-1 calls — Anthropic prompt caching keys on content,
not reference, so reading from the DB on every call still scores cache
hits as long as the skill body itself is stable.

Failure-soft: missing ANTHROPIC_API_KEY → returns None. JSON parse error,
invalid value, network error → returns None. The sweep stamps the
evidence row's scored_at either way (well, the sweep does — we just
return). NULL columns mean "scoring didn't apply"; the math layer
substitutes neutral multipliers.

Cost auditing: each call inserts a UsageEvent with
extra={"caller": "scenarios_scorer"} so the daily-budget guard in
sweep.py can sum spend separately from the Haiku classifier.
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
from .. import pricing, skills
from ..usage import current_run_id


DEFAULT_MODEL = "claude-sonnet-4-6"
SKILL_NAME = "predicate_scorer"
_MAX_FINDING_BODY_CHARS = 1500

_VALID_MECHANISM_PRESENT = ("yes", "no")
_VALID_MECHANISM_TYPE = ("pricing", "ux", "distribution", "trust", "other")
_VALID_BASE_RATE = ("high", "medium", "low")
_VALID_COUNTER = ("none", "weak", "strong")
_VALID_INCENTIVE = ("+", "-", "0")


class ScoredFields(NamedTuple):
    """All four dimensions in one tuple. None on any field means the
    LLM gave us something we couldn't validate; the math layer treats
    None as a neutral multiplier (=1.0) for that field."""
    mechanism_present: str | None       # "yes" | "no"
    mechanism_type: str | None          # pricing|ux|distribution|trust|other
    base_rate_bucket: str | None        # high|medium|low
    counter_evidence_strength: str | None  # none|weak|strong
    counter_evidence_example: str | None
    incentive_bias: str | None          # "+" | "-" | "0"


# ── Skill / system prompt loading ───────────────────────────────────────

def get_system_prompt() -> str:
    """Public accessor — used by tests, by sweep.py, and by the backfill
    script to pre-load the prompt once and pass it into every per-
    proposal call.

    Reads through app.skills.load_active so the admin /settings/skills
    surface can edit this prompt without a redeploy. Falls back to the
    file on disk if the DB row hasn't been seeded yet (cold start, fresh
    install). Returns an empty string only if neither source has the
    skill — the scorer treats empty as "skip", same as a missing API
    key, so legacy rows just don't get scored."""
    return skills.load_active(SKILL_NAME)


# ── Per-proposal user prompt ────────────────────────────────────────────

def _user_prompt(
    finding: dict,
    *,
    predicate_statement: str,
    target_state_label: str,
    direction: str,
    strength_bucket: str,
) -> str:
    """Per-proposal payload. Same structured-fields style as the
    classifier — Sonnet does fine with prose but the structure makes
    skipped/missing fields impossible to confuse."""
    title = (finding.get("title") or "").strip()
    summary = (finding.get("summary") or "").strip()
    content = (finding.get("content") or "").strip()
    body = summary or content[:_MAX_FINDING_BODY_CHARS]
    competitor = finding.get("competitor") or "unknown"
    source = finding.get("source") or "unknown"
    published = finding.get("published_at") or finding.get("created_at")
    pub_str = (
        published.strftime("%Y-%m-%d") if hasattr(published, "strftime")
        else str(published or "")
    )

    return (
        f"## Finding\n"
        f"Competitor: {competitor}\n"
        f"Source: {source}\n"
        f"Published: {pub_str}\n"
        f"Title: {title}\n\n"
        f"{body}\n\n"
        f"## Proposed mapping (already triaged — do not re-judge relevance)\n"
        f"Predicate statement: {predicate_statement}\n"
        f"Target state: {target_state_label}\n"
        f"Direction: {direction}\n"
        f"Strength bucket: {strength_bucket}\n\n"
        f"Score the four dimensions per the skill instructions. Return JSON only."
    )


# ── Output parsing ──────────────────────────────────────────────────────

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


def _validate_choice(value, allowed: tuple[str, ...]) -> str | None:
    """Return the value if it's in `allowed`, else None. Whitespace and
    case are tolerated; final value is the canonical lowercase form
    (except incentive_bias where '+'/'-'/'0' are already canonical)."""
    if value is None:
        return None
    s = str(value).strip()
    if s in allowed:
        return s
    s_low = s.lower()
    if s_low in allowed:
        return s_low
    return None


def parse_response(raw: str) -> ScoredFields | None:
    """Validate and normalize one Sonnet response. Returns None if the
    JSON shape is broken; returns a ScoredFields with per-field None
    when individual fields fail validation (so a partially-good response
    still contributes whatever it got right)."""
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        return None

    mech = parsed.get("mechanism") or {}
    base = parsed.get("base_rate") or {}
    counter = parsed.get("counter_evidence") or {}
    bias = parsed.get("incentive_bias") or {}

    mech_present = _validate_choice(
        mech.get("present") if isinstance(mech, dict) else None,
        _VALID_MECHANISM_PRESENT,
    )
    mech_type = _validate_choice(
        mech.get("type") if isinstance(mech, dict) else None,
        _VALID_MECHANISM_TYPE,
    )
    # type only meaningful when present == yes; null it otherwise.
    if mech_present != "yes":
        mech_type = None

    base_bucket = _validate_choice(
        base.get("bucket") if isinstance(base, dict) else None,
        _VALID_BASE_RATE,
    )

    counter_strength = _validate_choice(
        counter.get("strength") if isinstance(counter, dict) else None,
        _VALID_COUNTER,
    )
    counter_example = None
    if isinstance(counter, dict):
        ex = counter.get("example")
        if isinstance(ex, str) and ex.strip():
            counter_example = ex.strip()[:500]
    if counter_strength == "none":
        counter_example = None

    bias_value = _validate_choice(
        bias.get("value") if isinstance(bias, dict) else None,
        _VALID_INCENTIVE,
    )

    return ScoredFields(
        mechanism_present=mech_present,
        mechanism_type=mech_type,
        base_rate_bucket=base_bucket,
        counter_evidence_strength=counter_strength,
        counter_evidence_example=counter_example,
        incentive_bias=bias_value,
    )


# ── API call ────────────────────────────────────────────────────────────

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is not None:
        return _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    _client = anthropic.Anthropic()
    return _client


def get_model() -> str:
    """Configurable via env so a future Sonnet rev can be swapped without
    a code change. Defaults to claude-sonnet-4-6."""
    return os.environ.get("SCENARIOS_SCORER_MODEL", DEFAULT_MODEL)


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
                extra={"caller": "scenarios_scorer"},
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        traceback.print_exc()


def score_proposal(
    finding: dict,
    *,
    predicate_statement: str,
    target_state_label: str,
    direction: str,
    strength_bucket: str,
    system_prompt: str | None = None,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
) -> ScoredFields | None:
    """Single Sonnet call for one (finding, proposal) pair. Returns
    ScoredFields on success, None on any API or parse failure.

    `system_prompt` and `client` are injectable so the sweep can reuse
    one cached system prompt across many calls and so tests can stub the
    API. Both default to the production path.
    """
    if client is None:
        client = _get_client()
    if client is None:
        return None
    if model is None:
        model = get_model()
    if system_prompt is None:
        system_prompt = get_system_prompt()
    if not system_prompt:
        # No skill body in DB or on disk — happens on a fresh install
        # before the seed runs. Scorer can't grade without instructions
        # so we skip cleanly; legacy multipliers stay at 1.0.
        return None

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=400,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": _user_prompt(
                    finding,
                    predicate_statement=predicate_statement,
                    target_state_label=target_state_label,
                    direction=direction,
                    strength_bucket=strength_bucket,
                ),
            }],
        )
    except Exception:
        traceback.print_exc()
        return None

    _record_usage(model, resp)

    try:
        raw = resp.content[0].text
    except (AttributeError, IndexError):
        return None
    return parse_response(raw)
