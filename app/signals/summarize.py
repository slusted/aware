"""LLM-written snippets for stream cards.

Trafilatura extracts often lead with navigation/consent boilerplate
("[Skip to content](#wp--skip-link--target) [Startups](…) # Sources: …").
The stream card's 280-char slice of that is useless, so we ask Claude Haiku
to turn title + a trimmed slice of the extract into one or two plain-prose
sentences — no markdown, no quotes from nav links.

Kept deliberately small: haiku call, ~200 output tokens, best-effort.
On any failure we return None and the caller falls back to the raw content
(existing behavior), so this layer is fully optional to the pipeline.
"""

from __future__ import annotations

import os
import re

import anthropic


# Share the same model constant as analyzer.py — haiku is plenty for a
# one-sentence summary and keeps cost/latency negligible per finding.
MODEL = "claude-haiku-4-5-20251001"

# Hard caps so a pathological page can't blow up token spend or row size.
_MAX_INPUT_CHARS = 6000   # ~1.5k tokens of context, more than enough for a lede
_MAX_OUTPUT_CHARS = 320   # stream card shows ~280 chars; give a little headroom


# Patterns that show up in trafilatura output and carry no signal. We strip
# them before sending to the model so the prompt isn't "summarize this nav
# bar" and so the model has a better shot at finding the actual story.
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_BOILERPLATE_RE = re.compile(
    r"(skip to (main )?content|subscribe to (our )?newsletter|"
    r"sign up for|cookie (policy|preferences)|accept (all )?cookies|"
    r"share on (twitter|facebook|linkedin)|related articles?)",
    re.IGNORECASE,
)


def _strip_markdown_noise(text: str) -> str:
    """Flatten markdown links and drop obvious nav boilerplate lines.

    We keep the link *text* (it's sometimes the section heading) but lose
    the URL, and drop lines that are pure boilerplate. This is cheap and
    makes the LLM's job — and its output — meaningfully better.
    """
    # [label](url) → label
    text = _MD_LINK_RE.sub(r"\1", text)
    # Drop lines that are just boilerplate (whole-line match, case-insensitive).
    kept = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _BOILERPLATE_RE.search(line) and len(line) < 80:
            continue
        kept.append(line)
    return "\n".join(kept)


def _prompt(title: str | None, body: str, signal_type: str | None,
            competitor: str) -> str:
    signal_hint = f" The signal type is '{signal_type}'." if signal_type else ""
    title_line = f"Headline: {title}\n\n" if title else ""
    return (
        f"You write one-sentence snippets for a competitive-intelligence "
        f"feed card about {competitor}.{signal_hint}\n\n"
        f"{title_line}Article excerpt:\n{body}\n\n"
        f"Write a single plain-prose sentence (max 280 characters) that tells "
        f"the reader the concrete news: what happened, who is involved, the "
        f"key number or fact. No markdown. No hedging ('reportedly', 'appears'). "
        f"No lead-in like 'This article discusses'. If the excerpt is just "
        f"navigation boilerplate with no story, reply with the single word: SKIP."
    )


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic | None:
    """Lazy client — returns None if no API key is configured so the job
    pipeline keeps working (summary is optional, content is still saved)."""
    global _client
    if _client is not None:
        return _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    _client = anthropic.Anthropic()
    return _client


def summarize_finding(
    *,
    title: str | None,
    content: str | None,
    signal_type: str | None,
    competitor: str,
) -> str | None:
    """Return a clean, display-ready snippet for the stream card.

    Returns None when summarization is skipped or fails — the caller should
    fall back to the raw `content` field.
    """
    if not content or not content.strip():
        return None

    cleaned = _strip_markdown_noise(content)[:_MAX_INPUT_CHARS]
    if not cleaned.strip():
        return None

    client = _get_client()
    if client is None:
        return None

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": _prompt(title, cleaned, signal_type, competitor),
            }],
        )
        out = resp.content[0].text.strip()
    except Exception:
        # Any API / parsing error → fall back to raw content.
        return None

    if not out or out.upper().startswith("SKIP"):
        return None

    # Strip surrounding quotes the model sometimes adds, collapse whitespace,
    # and enforce the length cap so we never bloat the row.
    out = out.strip().strip('"').strip("'")
    out = re.sub(r"\s+", " ", out)
    if len(out) > _MAX_OUTPUT_CHARS:
        out = out[: _MAX_OUTPUT_CHARS - 1].rstrip() + "…"
    return out or None
