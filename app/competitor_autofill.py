"""
Competitor autofill — given a competitor name, run an Anthropic tool-use loop
with search + fetch exposed, have the model populate every CompetitorIn field,
and return a dict ready to drop into the New Competitor form.

Two public entry points:
  - autofill(name, ...) — blocking; returns the dict
  - autofill_stream(name, ...) — generator yielding progress events then a
    final {"type": "done", "data": ...} event. Used by the SSE endpoint so
    the UI can show each tool call as it happens.

The user reviews the filled form before saving; nothing is persisted here.
"""
from __future__ import annotations

import json
import os
from typing import Iterator

import anthropic

from app.search_providers.tavily import search_tavily
from app.fetcher import fetch_article

MODEL = "claude-sonnet-4-6"
MAX_TOOL_ROUNDS = 8
FETCH_CONTENT_CAP = 4000

_client = anthropic.Anthropic()

CATEGORIES = ["job_board", "ats", "labour_hire", "adjacent", "other"]

TOOLS = [
    {
        "name": "search_web",
        "description": (
            "Search the web via Tavily. Returns a list of {title, url, snippet, score}. "
            "Use this to discover the competitor's official site, newsroom, careers page, "
            "app store listings, subreddit activity, and threat positioning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "topic": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "Tavily topic. Default 'general'.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Number of results to return (default 5).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": (
            "Fetch and extract readable content from a URL. Use to confirm a newsroom "
            "subdomain, read an about page, or verify an app store listing ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute URL to fetch."}
            },
            "required": ["url"],
        },
    },
]


def _tool_search_web(query: str, topic: str = "general", max_results: int = 5) -> str:
    results = search_tavily(
        query,
        search_depth="advanced",
        topic=topic,
        max_results=max(1, min(10, int(max_results or 5))),
        include_raw=False,
    )
    trimmed = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("snippet") or r.get("content") or "")[:400],
            "score": r.get("score", 0),
        }
        for r in results
    ]
    return json.dumps(trimmed) if trimmed else "[]"


def _tool_fetch_url(url: str) -> str:
    content, source = fetch_article(url)
    if not content:
        return json.dumps({"url": url, "source": source, "content": ""})
    return json.dumps({
        "url": url,
        "source": source,
        "content": content[:FETCH_CONTENT_CAP],
    })


def _run_tool(name: str, args: dict) -> str:
    try:
        if name == "search_web":
            return _tool_search_web(
                args.get("query", ""),
                args.get("topic", "general"),
                args.get("max_results", 5),
            )
        if name == "fetch_url":
            return _tool_fetch_url(args.get("url", ""))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    return json.dumps({"error": f"unknown tool {name}"})


def _build_system_prompt(
    company: str,
    industry: str,
    existing: dict | None = None,
    performance_report: str | None = None,
) -> str:
    existing_block = ""
    if existing:
        cleaned = {k: v for k, v in existing.items() if v not in (None, "", [])}
        if cleaned:
            existing_block = (
                "\nThis competitor is ALREADY TRACKED. Here are the current "
                "field values — your job is to refine and extend them, not "
                "replace with generic fill. Keep any existing entries that "
                "are specific and useful; add new ones you discover; rewrite "
                "a field only when you can clearly improve it. If a field "
                "already looks good, return it unchanged.\n\n"
                f"Current values:\n{json.dumps(cleaned, indent=2)}\n\n"
            )
    # Performance report is only present in edit-mode when the competitor
    # has findings history. Injected verbatim so the agent reasons over the
    # same text the operator would see.
    report_block = ""
    if performance_report:
        report_block = (
            "\n\n---\n"
            f"{performance_report}\n"
            "---\n\n"
            "Tuning rubric (apply to the fields above):\n"
            "\n"
            "Verdicts are derived from two independent signals:\n"
            "- Digest inclusion — HIGH/MEDIUM/LOW/NOISE is the market-digest "
            "analyst's per-finding threat label. HIGH and MEDIUM mean the finding "
            "was featured as a real competitive signal. NOISE means the analyst "
            "explicitly called it stale or off-topic.\n"
            "- Signal-type diversity — a keyword producing product_launch + "
            "funding + new_hire is broader than one producing only 'news'.\n"
            "\n"
            "Rules:\n"
            "- Keywords marked STRONG: keep.\n"
            "- Keywords marked WEAK: replace or drop. Propose a more specific "
            "alternative verified via search_web — prefer sub-product or feature "
            "names (e.g. 'LinkedIn Recruiter AI' over 'LinkedIn hiring').\n"
            "- Keywords marked OK: keep unless you find a clearly better replacement.\n"
            "- Keywords marked UNSCORED: keep for now — they haven't been through "
            "a digest cycle yet. Revisit next time.\n"
            "- Keywords in the 'Silent' list: drop unless search_web surfaces "
            "new evidence of recent activity worth tracking.\n"
            "- Subreddits in the 'Silent' list: drop unless search_web surfaces "
            "recent (last 60d) discussion there.\n"
            "- Low avg-materiality sources: check whether configured domains "
            "(newsroom_domains / careers_domains) are still right; verify with "
            "fetch_url before correcting.\n"
            "- Do NOT invent replacements. Every addition must be traceable to "
            "a tool result from this session.\n"
        )
    # Edit mode (existing != None) now returns a list of proposed changes
    # with rationale, so the user can accept or reject each one. New-competitor
    # mode still returns a flat CompetitorIn dict — no old values to diff.
    is_edit = existing is not None
    fields_spec = (
        f"Fields:\n"
        f"- category: one of {CATEGORIES}\n"
        f"- threat_angle: one or two sentences on why {company} should care\n"
        f"- keywords (list): 3–6 specific search terms; prefer sub-products over "
        f"the bare company name (e.g. 'LinkedIn Recruiter' > 'LinkedIn')\n"
        f"- subreddits (list): names without the r/ prefix, only if they "
        f"actually discuss this competitor\n"
        f"- careers_domains (list): domains hosting their job listings; empty if unknown\n"
        f"- newsroom_domains (list): newsroom.example.com / press.example.com; empty if none\n"
        f"- homepage_domain: their canonical apex domain (e.g. 'linkedin.com', not "
        f"'www.linkedin.com' or 'careers.linkedin.com'). Used for the company logo "
        f"and as a stable identifier. Must be reachable — verify via fetch_url.\n"
        f"- app_store_id: numeric iOS App Store id (as a string); null if none\n"
        f"- play_package: Google Play package name like com.example.app; null if none\n"
        f"- trends_keyword: Google Trends keyword override, usually the brand name\n"
        f"- min_relevance_score: leave null unless the competitor is notoriously noisy\n"
        f"- social_score_multiplier: leave null unless social noise likely dominates\n"
    )

    if is_edit:
        output_spec = (
            "## Output format — PROPOSALS\n\n"
            "Respond with ONLY a JSON object of this shape (no prose, no "
            "markdown fences):\n"
            "{\n"
            '  "proposals": [\n'
            '    {"action": "replace", "field": "<scalar field>", '
            '"old_value": <current>, "value": <new>, '
            '"rationale": "<why, 1-2 sentences>", '
            '"evidence_url": "<url you fetched or searched>"},\n'
            '    {"action": "add", "field": "<list field>", '
            '"value": "<new item>", '
            '"rationale": "...", "evidence_url": "..."},\n'
            '    {"action": "drop", "field": "<list field>", '
            '"value": "<existing item being removed>", '
            '"rationale": "...", "evidence_url": null}\n'
            "  ]\n"
            "}\n\n"
            "Rules for proposals:\n"
            "- One proposal per change. For list fields (keywords, subreddits, "
            "careers_domains, newsroom_domains) use action='add' for additions "
            "and action='drop' for removals; a 'replacement' is two proposals "
            "(drop old + add new) with matching rationale.\n"
            "- For scalar fields (category, threat_angle, app_store_id, "
            "play_package, trends_keyword, min_relevance_score, "
            "social_score_multiplier) use action='replace' with both old_value "
            "and value.\n"
            "- Emit NO proposal for fields you want to keep unchanged. An empty "
            "proposals array is a valid answer when nothing should change.\n"
            "- Every proposal MUST have a rationale. Every non-drop proposal "
            "SHOULD have an evidence_url (a URL from your search_web or "
            "fetch_url results). Drops can have evidence_url: null when the "
            "justification is the performance report (e.g. '0 hits in 60d').\n"
            "- Be conservative. Do not propose speculative changes. UNSCORED "
            "keywords should usually be kept, not dropped.\n"
            "- Do NOT invent values. Every add/replace must be traceable to a "
            "tool call you made in this session."
        )
    else:
        output_spec = (
            "When you have enough evidence, respond with ONLY a JSON object "
            "matching the CompetitorIn schema — no prose, no markdown fences. "
            "Unknown fields should be null (scalars) or [] (lists). Never "
            "hallucinate an App Store ID or play package — if you cannot "
            "confirm it via search or fetch, return null."
        )

    return (
        f"You are a competitive-intelligence analyst for {company} in the "
        f"{industry} industry. A colleague is working on the competitor form. "
        f"Use search_web and fetch_url to research the competitor — their "
        f"official site, newsroom, careers page, iOS/Android apps, and any "
        f"subreddit where their users hang out. Do 2–6 rounds of tool calls, "
        f"then return your answer."
        f"{existing_block}{report_block}\n\n"
        f"{fields_spec}\n"
        f"{output_spec}"
    )


def _extract_json(text: str) -> dict | None:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


_LIST_FIELDS = {"keywords", "subreddits", "careers_domains", "newsroom_domains"}
_SCALAR_FIELDS = {
    "category", "threat_angle", "homepage_domain", "app_store_id", "play_package",
    "trends_keyword", "min_relevance_score", "social_score_multiplier",
}
_ALL_FIELDS = _LIST_FIELDS | _SCALAR_FIELDS


def _normalize_proposals(payload: dict, existing: dict) -> list[dict]:
    """Validate and coerce the agent's proposal list. Drops anything
    malformed rather than surfacing it to the UI — a broken proposal
    becomes invisible, which is better than a misleading diff card.

    Each returned proposal has the shape:
      {action, field, value, old_value, rationale, evidence_url}
    - action: 'replace' (scalars) | 'add' (list fields) | 'drop' (list fields)
    - old_value is only set for replace/drop; for add it's null.
    - value is the new scalar (replace), the item to add (add),
      or the item being removed (drop).
    """
    raw = payload.get("proposals")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip().lower()
        field = str(item.get("field") or "").strip()
        if field not in _ALL_FIELDS:
            continue
        if action not in ("replace", "add", "drop"):
            continue
        if action == "replace" and field not in _SCALAR_FIELDS:
            continue
        if action in ("add", "drop") and field not in _LIST_FIELDS:
            continue
        rationale = str(item.get("rationale") or "").strip()
        if not rationale:
            continue  # no rationale = no diff card; agent must justify every change
        value = item.get("value")
        if value is None and action != "replace":
            continue
        if action in ("add", "drop"):
            value = str(value).strip()
            if not value:
                continue
        # For replace, retain old_value from the agent's payload if sane,
        # otherwise fall back to the current `existing` dict so the diff card
        # never lies about what's being overwritten.
        old_value = item.get("old_value")
        if action == "replace":
            if old_value in (None, ""):
                old_value = existing.get(field)
        elif action == "drop":
            # For list drops, old_value is effectively the item being dropped.
            old_value = value
        else:
            old_value = None
        evidence_url = item.get("evidence_url")
        if evidence_url is not None:
            evidence_url = str(evidence_url).strip() or None
        out.append({
            "action": action,
            "field": field,
            "value": value,
            "old_value": old_value,
            "rationale": rationale,
            "evidence_url": evidence_url,
        })
    return out


def _normalize(payload: dict, name: str) -> dict:
    """Coerce the agent's JSON into CompetitorIn shape. Never trust types blindly."""
    def _as_list(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    def _as_str_or_none(v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    def _as_float_or_none(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    category = _as_str_or_none(payload.get("category"))
    if category not in CATEGORIES:
        category = None

    subreddits = [s.lstrip("r/").lstrip("/") for s in _as_list(payload.get("subreddits"))]

    # homepage_domain: strip scheme + path if the model accidentally returns a
    # URL. Keep dots and hyphens; reject anything else — the cache layer also
    # validates, but catching garbage here gives the operator a clean form.
    hd = _as_str_or_none(payload.get("homepage_domain"))
    if hd:
        if "//" in hd:
            hd = hd.split("//", 1)[1]
        hd = hd.split("/", 1)[0].lower()
        import re as _re
        if not _re.match(r"^[a-z0-9][a-z0-9.-]*$", hd):
            hd = None

    return {
        "name": name,
        "category": category,
        "threat_angle": _as_str_or_none(payload.get("threat_angle")),
        "keywords": _as_list(payload.get("keywords")),
        "subreddits": subreddits,
        "careers_domains": _as_list(payload.get("careers_domains")),
        "newsroom_domains": _as_list(payload.get("newsroom_domains")),
        "homepage_domain": hd,
        "app_store_id": _as_str_or_none(payload.get("app_store_id")),
        "play_package": _as_str_or_none(payload.get("play_package")),
        "trends_keyword": _as_str_or_none(payload.get("trends_keyword")),
        "min_relevance_score": _as_float_or_none(payload.get("min_relevance_score")),
        "social_score_multiplier": _as_float_or_none(payload.get("social_score_multiplier")),
    }


def _describe_tool_call(tool_name: str, args: dict) -> str:
    """Short human-readable label for the UI progress line."""
    if tool_name == "search_web":
        q = (args.get("query") or "").strip()
        topic = args.get("topic") or "general"
        return f"searching ({topic}): {q[:80]}"
    if tool_name == "fetch_url":
        url = (args.get("url") or "").strip()
        return f"fetching: {url[:100]}"
    return tool_name


def autofill_stream(
    name: str,
    company: str,
    industry: str,
    existing: dict | None = None,
    performance_report: str | None = None,
) -> Iterator[dict]:
    """Generator variant — yields progress events, ending with either
    {"type": "done", "data": <CompetitorIn dict>} or {"type": "error", ...}.

    If `existing` is provided (edit-mode), the agent is told to refine + extend
    those values rather than start blank. `performance_report` (edit-mode only)
    is a plain-text block of finding-history stats from
    app.competitor_performance.build_performance_report — when present the
    agent is told to treat it as the tuning rubric.

    Never raises; fatal problems are surfaced as error events so the SSE
    stream always terminates cleanly."""
    name = (name or "").strip()
    if not name:
        yield {"type": "error", "message": "competitor name is required"}
        return

    mode = "tuning from history" if performance_report else ("refining" if existing else "starting")
    yield {"type": "progress", "message": f"{mode} research for {name}"}

    if performance_report:
        user_msg = (
            f"Tune the current field values for '{name}' based on the "
            f"performance report in the system prompt. Prune keywords and "
            f"subreddits marked WEAK or Silent; propose specific replacements "
            f"verified via search_web. Keep STRONG entries unchanged."
        )
    elif existing:
        user_msg = (
            f"Refine and extend the current field values for '{name}'. Only "
            f"change fields where your research clearly improves them. Start "
            f"by checking for recent news or product changes."
        )
    else:
        user_msg = (
            f"Research '{name}' and fill in every field for the new-competitor "
            f"form. Start by searching for their official site and recent news."
        )
    messages = [{"role": "user", "content": user_msg}]
    system = _build_system_prompt(
        company, industry, existing=existing,
        performance_report=performance_report,
    )
    final_text = ""

    try:
        for round_idx in range(MAX_TOOL_ROUNDS):
            yield {"type": "progress", "message": f"thinking (round {round_idx + 1})"}
            resp = _client.messages.create(
                model=MODEL,
                max_tokens=2000,
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
                "content": "Stop researching. Return the JSON object now with whatever you've found.",
            })
            resp = _client.messages.create(
                model=MODEL,
                max_tokens=1500,
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
    # Edit mode returns a proposals list the UI renders as accept/reject
    # cards. New-mode still emits a CompetitorIn-shaped dict that fills the
    # form directly — there's no 'old value' to diff in that case.
    if existing is not None:
        proposals = _normalize_proposals(payload, existing)
        yield {
            "type": "done",
            "mode": "proposals",
            "proposals": proposals,
            "name": name,
        }
    else:
        data = _normalize(payload, name)
        yield {"type": "done", "mode": "autofill", "data": data}


def autofill(
    name: str,
    company: str,
    industry: str,
    existing: dict | None = None,
    performance_report: str | None = None,
) -> dict:
    """Blocking variant — drains the stream and returns the final payload.

    Returns the raw `done` event body: `{"mode": "autofill", "data": {...}}`
    for new-competitor runs or `{"mode": "proposals", "proposals": [...]}`
    for edit-mode runs. Callers branch on `mode`.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("competitor name is required")
    for event in autofill_stream(
        name, company, industry,
        existing=existing, performance_report=performance_report,
    ):
        if event["type"] == "error":
            raise RuntimeError(event["message"])
        if event["type"] == "done":
            return event
    return {"mode": "autofill", "data": _normalize({}, name)}
