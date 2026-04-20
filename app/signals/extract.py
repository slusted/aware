"""Signal classification — turns a raw finding into a typed signal.

`classify()` returns (signal_type, materiality, payload) for a finding
dict. Today the classifier is rule-based: regex on title + content,
topic-matching, source heuristics. Deterministic, fast, easy to reason
about. An LLM pass can replace or supplement this later without
touching the call sites — the return contract stays the same.

Taxonomy (keep in sync with Finding.signal_type docstring in models.py):
    news | price_change | new_hire | product_launch | messaging_shift |
    funding | integration | voc_mention | momentum_point | other

Materiality is the 0–1 float the stream UI filters on. Rule defaults
encode a rough "how likely is this worth surfacing" — they're a first
pass, not a commitment. Tune based on what clutters the stream in
practice.
"""
from __future__ import annotations

import re


_ROLE_RE = re.compile(
    r"\b(CEO|CFO|COO|CTO|CMO|CPO|CIO|VP|SVP|EVP|chief|president|head of|director)\b",
    re.IGNORECASE,
)
_HIRE_VERBS = re.compile(
    r"\b(hires?|hired|appoints?|appointed|names?(?= \w+ as| as)|joins? as|promoted to|taps? \w+ as)\b",
    re.IGNORECASE,
)
_FUNDING_RE = re.compile(
    r"\b(raises?\s+\$?\d|raised\s+\$?\d|secures?\s+\$?\d|series\s+[A-E]\b|funding round|closes?\s+[\$\w\s]*round|\$\d+(?:\.\d+)?\s*(?:M|B|million|billion)\b)",
    re.IGNORECASE,
)
_LAUNCH_RE = re.compile(
    r"\b(launches?|launched|introduces?|introducing|unveils?|unveiled|rolls?\s+out|released?\s+(?:today|new))\b",
    re.IGNORECASE,
)
_INTEGRATION_RE = re.compile(
    r"\b(partners?\s+with|partnership\s+with|integrates?\s+with|integration\s+with|acquires?|acquired|acquisition\s+of)\b",
    re.IGNORECASE,
)
_PRICING_RE = re.compile(
    r"\b(price\s+(?:hike|drop|increase|cut|change)|pricing\s+(?:update|change)|new\s+(?:plan|tier|pricing)|subscription\s+(?:cost|price))\b",
    re.IGNORECASE,
)


def classify(finding: dict) -> tuple[str, float, dict]:
    """Return (signal_type, materiality, payload) for a finding.

    Order matters — specific categories beat the default 'news' bucket.
    VoC is detected by `topic` first since it's stamped upstream and
    doesn't depend on text content.
    """
    topic = (finding.get("topic") or "").lower()
    source = (finding.get("source") or "").lower()
    title = finding.get("title") or ""
    content = finding.get("content") or ""
    # Title + first 2 KB of body — enough for headline-style signals without
    # scanning entire long-form pages (which would produce many false matches).
    haystack = f"{title}\n{content[:2000]}"

    if topic == "voice of customer" or source.startswith("reddit/") or source == "linkedin":
        return "voc_mention", 0.4, {}

    # Careers pages: the "About" blurb often name-drops a recent round
    # (e.g. "raised $100M Series B"), which would otherwise false-match
    # the funding regex. Short-circuit before the regex chain.
    if source == "careers" or topic == "strategic hiring":
        return "new_hire", 0.5, {"matched": "careers_source"}

    if _FUNDING_RE.search(haystack):
        return "funding", 0.9, {"matched": "funding_regex"}
    if _ROLE_RE.search(haystack) and _HIRE_VERBS.search(haystack):
        return "new_hire", 0.8, {"matched": "hire_role_regex"}
    if _LAUNCH_RE.search(haystack):
        return "product_launch", 0.7, {"matched": "launch_regex"}
    if _INTEGRATION_RE.search(haystack):
        return "integration", 0.6, {"matched": "integration_regex"}
    if _PRICING_RE.search(haystack):
        return "price_change", 0.7, {"matched": "pricing_regex"}

    return "news", 0.3, {}


def stamp(finding: dict) -> dict:
    """Mutate a finding dict in place, adding signal_type/materiality/payload.
    Returns the same dict so calls can chain."""
    st, mat, payload = classify(finding)
    finding["signal_type"] = st
    finding["materiality"] = mat
    finding["payload"] = payload
    return finding
