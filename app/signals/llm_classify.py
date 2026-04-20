"""LLM classifier + summarizer for findings.

One Haiku call returns signal_type, materiality, and a stream-ready
summary in a single round trip. Regex classifier in extract.py is the
fallback path when the API key is missing or the call fails.

Design notes:
- Content window is 8KB per finding (vs the regex path's 2KB haystack).
  The extra context lets the model distinguish e.g. a job posting that
  namedrops a funding round in its "About" blurb from an actual funding
  announcement — the exact failure mode the regex classifier hits.
- System prompt carries the taxonomy + materiality rubric and is marked
  for prompt caching; the user block varies per finding. Most scan runs
  fire many calls back-to-back so the cache hit rate is high when the
  prompt is over the 2048-token minimum.
- JSON is parsed with a tolerant extractor (strips code fences, finds
  the first {...} block). On any failure we return None and the caller
  falls back to the regex classifier + raw content.
"""

from __future__ import annotations

import json
import os
import re

import anthropic


MODEL = "claude-haiku-4-5-20251001"

_MAX_INPUT_CHARS = 8000
_MAX_SUMMARY_CHARS = 320

_VALID_TYPES = {
    "news", "price_change", "new_hire", "product_launch",
    "messaging_shift", "funding", "integration", "voc_mention",
    "momentum_point", "other",
}

_SYSTEM_PROMPT = """You are a competitive-intelligence classifier. For each finding you receive you must return a JSON object with three fields: signal_type, materiality, and summary.

## signal_type (string, required)

Pick exactly one from this taxonomy. Order of preference matters — apply specific categories before the generic ones.

- funding: the competitor raised capital, closed a round, or had an explicit valuation event. NOT a job posting that mentions a prior round in its "About" blurb.
- new_hire: the competitor hired a named senior person (VP/Director/C-level/Head of). A job posting (open role on a careers page) also maps here — it signals hiring intent.
- product_launch: a new product, feature, or major release went live or was announced.
- price_change: pricing, plans, or subscription costs changed.
- integration: a new partnership, integration, or acquisition (they acquired or were acquired).
- messaging_shift: the competitor's positioning, homepage copy, or public narrative materially changed.
- voc_mention: voice-of-customer — a user/customer post on Reddit, LinkedIn, etc. discussing the competitor.
- momentum_point: traffic, app-ranking, or engagement datapoint (usually stamped upstream).
- news: general press coverage that doesn't fit above.
- other: genuinely uncategorizable.

The finding's `source` and `topic` fields are strong hints. In particular:
- source="careers" or topic="strategic hiring" → new_hire (it's a job posting)
- source starting with "reddit/" or source="linkedin" → voc_mention
- topic="voice of customer" → voc_mention

## materiality (float 0.0–1.0, required)

How worth-surfacing is this for a competitive-intel stream reader?
- 0.9+: funding round, acquisition, major exec hire, flagship launch
- 0.6–0.8: notable launch, pricing change, integration, named senior hire
- 0.4–0.5: job posting, general press, minor launch, VoC mention
- 0.0–0.3: routine news, low-signal content

## summary (string, required)

One plain-prose sentence (max 280 characters) telling the reader the concrete news: what happened, who is involved, the key number or fact. No markdown. No hedging ("reportedly", "appears"). No lead-ins like "This article discusses". If the excerpt is just navigation boilerplate with no real story, return the literal string "SKIP" for summary (but still provide signal_type and materiality based on the title).

## Output format

Return ONLY a JSON object, no prose, no code fences:
{"signal_type": "...", "materiality": 0.0, "summary": "..."}
"""


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_BOILERPLATE_RE = re.compile(
    r"(skip to (main )?content|subscribe to (our )?newsletter|"
    r"sign up for|cookie (policy|preferences)|accept (all )?cookies|"
    r"share on (twitter|facebook|linkedin)|related articles?)",
    re.IGNORECASE,
)


def _strip_markdown_noise(text: str) -> str:
    text = _MD_LINK_RE.sub(r"\1", text)
    kept = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _BOILERPLATE_RE.search(line) and len(line) < 80:
            continue
        kept.append(line)
    return "\n".join(kept)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Pull the first {...} block out of the response and json.loads it."""
    text = text.strip()
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


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is not None:
        return _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    _client = anthropic.Anthropic()
    return _client


def _user_prompt(finding: dict, competitor: str, cleaned_body: str) -> str:
    title = finding.get("title") or ""
    source = finding.get("source") or ""
    topic = finding.get("topic") or ""
    url = finding.get("url") or ""
    return (
        f"Competitor: {competitor}\n"
        f"Source: {source}\n"
        f"Topic: {topic}\n"
        f"URL: {url}\n"
        f"Title: {title}\n\n"
        f"Content:\n{cleaned_body}"
    )


def classify_and_summarize(
    finding: dict,
    competitor: str,
) -> dict | None:
    """Classify and summarize a finding in one Haiku call.

    Returns a dict with keys signal_type, materiality, summary, payload —
    or None if the LLM is unavailable or the call fails. Callers should
    fall back to the regex classifier in extract.py on None.
    """
    content = finding.get("content") or ""
    if not content.strip():
        return None

    client = _get_client()
    if client is None:
        return None

    cleaned = _strip_markdown_noise(content)[:_MAX_INPUT_CHARS]
    if not cleaned.strip():
        return None

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": _user_prompt(finding, competitor, cleaned),
            }],
        )
        raw = resp.content[0].text
    except Exception:
        return None

    parsed = _extract_json(raw)
    if not parsed:
        return None

    st = parsed.get("signal_type")
    if st not in _VALID_TYPES:
        return None

    try:
        mat = float(parsed.get("materiality", 0.0))
    except (TypeError, ValueError):
        return None
    mat = max(0.0, min(1.0, mat))

    summary = parsed.get("summary")
    if isinstance(summary, str):
        summary = summary.strip().strip('"').strip("'")
        summary = re.sub(r"\s+", " ", summary)
        if summary.upper().startswith("SKIP") or not summary:
            summary = None
        elif len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[: _MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    else:
        summary = None

    return {
        "signal_type": st,
        "materiality": mat,
        "summary": summary,
        "payload": {"matched": "llm"},
    }
