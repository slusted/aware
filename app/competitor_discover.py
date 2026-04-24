"""Competitor discovery — given our company + industry + an exclusion list,
run an Anthropic tool-use loop with search + fetch exposed, and return a
list of candidate competitors we're not already tracking.

Shape mirrors app/competitor_autofill.py — same model, same tools, same
extraction helpers. Different system prompt, different output schema
(a list of candidates rather than one filled form).
"""
from __future__ import annotations

import json
from typing import Iterator

import anthropic

from .competitor_autofill import (
    TOOLS,
    _extract_json,
    _run_tool,
    _describe_tool_call,
)
from .skills import load_active

MODEL = "claude-sonnet-4-6"
MAX_TOOL_ROUNDS = 10
MAX_CANDIDATES = 8

CATEGORIES = ["job_board", "ats", "labour_hire", "adjacent", "other"]

_client = anthropic.Anthropic()


def _render_skill(
    our_company: str,
    our_industry: str,
    existing: list[str],
    dismissed: list[str],
    hint: str | None,
) -> str:
    """Fill the discover_competitors_brief skill template. Falls back to a
    minimal built-in prompt if the skill file is missing, so discovery
    still works on a fresh install before the seed pass runs."""
    template = load_active("discover_competitors") or _FALLBACK_PROMPT
    vals = {
        "our_company":    our_company or "our company",
        "our_industry":   our_industry or "our industry",
        "existing_list":  "\n".join(f"- {d}" for d in existing) or "- (none)",
        "dismissed_list": "\n".join(f"- {d}" for d in dismissed) or "- (none)",
        "hint":           (hint or "").strip(),
    }
    out = template
    for k, v in vals.items():
        out = out.replace("{{" + k + "}}", v)
    # Strip the optional `{{#hint}}...{{/hint}}` block when no hint is
    # provided. Keep it (sans tags) otherwise.
    if vals["hint"]:
        out = out.replace("{{#hint}}", "").replace("{{/hint}}", "")
    else:
        out = _strip_block(out, "{{#hint}}", "{{/hint}}")
    return out


def _strip_block(text: str, open_tag: str, close_tag: str) -> str:
    while True:
        i = text.find(open_tag)
        if i < 0:
            return text
        j = text.find(close_tag, i)
        if j < 0:
            return text[:i]
        text = text[:i] + text[j + len(close_tag):]


_FALLBACK_PROMPT = """\
You are a competitive-intelligence analyst for {{our_company}} in {{our_industry}}.

Find up to 8 companies we should consider adding to our competitor watchlist
that we are NOT already tracking. Use search_web and fetch_url to discover
candidates and confirm each one is a real, operating business.

Already tracked (do not return):
{{existing_list}}

Previously dismissed (do not return):
{{dismissed_list}}

{{#hint}}Focus: {{hint}}{{/hint}}

For each candidate return: name, homepage_domain (apex, verified), category
from [job_board, ats, labour_hire, adjacent, other], a one-sentence
one_line_why, and up to 5 evidence items of {title, url}.

Respond with ONLY a JSON object:
{"candidates": [{"name":"...","homepage_domain":"...","category":"...","one_line_why":"...","evidence":[{"title":"...","url":"..."}]}]}
"""


def _normalize_domain(raw) -> str | None:
    """Same shape as the autofill path — strip scheme / path / www,
    lowercase, validate charset. Return None if unusable for dedup."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    import re
    if not re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", s):
        return None
    return s


def _normalize_candidates(payload: dict, exclude: set[str]) -> list[dict]:
    """Coerce the agent's candidate list into clean dicts. Drops entries
    missing a name or homepage domain, entries whose domain is excluded,
    and entries past the cap. Dedupes within the same response."""
    raw = payload.get("candidates")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        domain = _normalize_domain(item.get("homepage_domain"))
        if not domain:
            # Keep the row but with a null domain — the UI will still show
            # it; the operator can dismiss or decide whether to research.
            # Skip dedup check in this case.
            pass
        elif domain in exclude or domain in seen:
            continue
        if domain:
            seen.add(domain)

        category = str(item.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            category = None

        why = str(item.get("one_line_why") or "").strip()

        evidence: list[dict] = []
        ev_raw = item.get("evidence")
        if isinstance(ev_raw, list):
            for ev in ev_raw[:5]:
                if not isinstance(ev, dict):
                    continue
                url = str(ev.get("url") or "").strip()
                title = str(ev.get("title") or "").strip() or url
                if url:
                    evidence.append({"title": title, "url": url})

        out.append({
            "name": name,
            "homepage_domain": domain,
            "category": category,
            "one_line_why": why,
            "evidence": evidence,
        })
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def discover_stream(
    our_company: str,
    our_industry: str,
    existing: list[str],
    dismissed: list[str],
    hint: str | None = None,
) -> Iterator[dict]:
    """Generator — yields progress events ending with either
    {"type": "done", "candidates": [...]} or {"type": "error", "message": ...}.

    Never raises; fatal errors are emitted as error events so callers can
    drive an SSE stream or collect the list via the blocking wrapper
    without handling exceptions.
    """
    exclude = {d for d in (list(existing) + list(dismissed)) if d}

    if hint:
        yield {"type": "progress", "message": f"discovering with focus: {hint[:80]}"}
    else:
        yield {"type": "progress", "message": "discovering new competitors"}

    system = _render_skill(our_company, our_industry, existing, dismissed, hint)
    user_msg = (
        f"Find up to {MAX_CANDIDATES} candidate competitors for "
        f"{our_company}. Start with a broad search, then verify each "
        f"candidate's homepage with fetch_url. Skip anyone already in "
        f"the 'already tracked' or 'previously dismissed' lists above."
    )
    messages = [{"role": "user", "content": user_msg}]
    final_text = ""

    try:
        for round_idx in range(MAX_TOOL_ROUNDS):
            yield {"type": "progress", "message": f"thinking (round {round_idx + 1})"}
            resp = _client.messages.create(
                model=MODEL,
                max_tokens=2500,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                for block in resp.content:
                    if getattr(block, "type", None) == "text":
                        final_text += block.text
                break

            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    label = _describe_tool_call(block.name, block.input or {})
                    yield {"type": "progress", "message": label}
                    result = _run_tool(block.name, block.input or {})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            if not tool_results:
                break
            messages.append({"role": "user", "content": tool_results})
        else:
            yield {"type": "progress", "message": "wrapping up — asking for final JSON"}
            messages.append({
                "role": "user",
                "content": "Stop researching. Return the JSON object now with the candidates you've found.",
            })
            resp = _client.messages.create(
                model=MODEL,
                max_tokens=2000,
                system=system,
                messages=messages,
            )
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    final_text += block.text
    except Exception as e:
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
        return

    payload = _extract_json(final_text) or {}
    candidates = _normalize_candidates(payload, exclude)
    yield {"type": "done", "candidates": candidates}


def discover(
    our_company: str,
    our_industry: str,
    existing: list[str],
    dismissed: list[str],
    hint: str | None = None,
) -> list[dict]:
    """Blocking variant — drains the stream and returns the candidate list."""
    for event in discover_stream(
        our_company, our_industry, existing, dismissed, hint=hint,
    ):
        if event["type"] == "error":
            raise RuntimeError(event["message"])
        if event["type"] == "done":
            return event.get("candidates", [])
    return []
