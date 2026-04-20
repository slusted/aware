"""
Deepen — agentic follow-up pass over the scripted scan's findings.

The scripted scanner runs a fixed 4-template matrix per keyword (see
scanner.scan_competitor). That's great for breadth but blind to follow-up:
if a finding mentions "rumored Series C" we can't chase it. This module
closes that gap without rewriting the pipeline.

Flow:
  1. Receive findings from run_full_scan.
  2. Show Claude a compact summary (title + snippet, grouped by competitor).
  3. Expose two tools: search_web and record_finding.
  4. Let the model issue up to DEEPEN_MAX_SEARCHES follow-up queries
     (default 3) and promote up to DEEPEN_MAX_NEW_FINDINGS results
     (default 6) into the digest.
  5. Return the merged finding list.

The scripted scan's min_relevance/dedup/noise filters still run inside
dispatch_search before results reach the model — the model can't surface
content Python already rejected as duplicate or chrome.

Fails soft: any API error, budget exhaustion, or malformed tool call
returns the original findings unchanged. Scripted scan is the source of
truth; deepen is additive only.
"""

import os
import json
import anthropic

from scanner import (
    search_tavily,
    is_new,
    mark_seen,
    is_noise,
    is_chrome,
    _is_garbage_title,
    current_min_relevance,
)
from app.adapters.fetch.sanitize import EXCLUDE_DOMAINS


MODEL = "claude-sonnet-4-6"
_client = anthropic.Anthropic()


def _budget(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


TOOLS = [
    {
        "name": "search_web",
        "description": (
            "Run a Tavily web search. Use this to chase a lead in the "
            "findings — e.g. a rumored acquisition, a vague hiring signal, "
            "a half-reported product launch. Prefer targeted queries "
            '(site: filters, exact phrases in quotes, year-scoped). '
            "Avoid restating queries the scripted scan would have run: "
            "generic '<competitor> product launch' is already covered. "
            "Returns up to 5 results (title + snippet + url + relevance score)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "topic": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "Use 'news' for recent headlines, 'general' otherwise.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "record_finding",
        "description": (
            "Promote a search result into today's digest. Only record items "
            "that materially add to what the scripted scan already found — "
            "a new angle, a confirmation of a rumor, a detail worth the "
            "analyst's time. Include a short rationale for why this matters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "competitor": {"type": "string"},
                "topic": {
                    "type": "string",
                    "description": "Short label, e.g. 'acquisition rumor', 'hiring surge', 'pricing change'.",
                },
                "title": {"type": "string"},
                "url": {"type": "string"},
                "content": {"type": "string", "description": "The actual snippet/content from the search result."},
                "rationale": {"type": "string", "description": "1 sentence on why this is worth surfacing."},
            },
            "required": ["competitor", "topic", "content"],
        },
    },
    {
        "name": "stop",
        "description": "Call when further searching has diminishing returns. Required to finish.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _summarise_findings(findings: list[dict], per_competitor_cap: int = 8) -> str:
    """One compact line per finding, grouped by competitor. Title + short snippet."""
    if not findings:
        return "(no findings from scripted scan)"

    by_comp: dict[str, list[dict]] = {}
    for f in findings:
        by_comp.setdefault(f["competitor"], []).append(f)

    lines = []
    for comp, items in by_comp.items():
        items = sorted(items, key=lambda x: x.get("relevance", 0), reverse=True)[:per_competitor_cap]
        lines.append(f"\n## {comp} ({len(items)} items)")
        for f in items:
            title = (f.get("title") or "").strip()[:100]
            snippet = (f.get("snippet") or f.get("content") or "")[:180].replace("\n", " ").strip()
            src = f.get("source", "?")
            topic = f.get("topic", "?")
            lines.append(f"- [{src}/{topic}] {title} — {snippet}")
    return "\n".join(lines)


def _run_search(query: str, topic: str, memory: dict) -> list[dict]:
    """Execute a model-requested search. Apply the same noise/dedup filters
    the scripted scan uses — the model must not see content Python already
    rejected as duplicate or chrome."""
    min_score = current_min_relevance()
    try:
        results = search_tavily(
            query,
            search_depth="advanced",
            topic=topic if topic in ("general", "news") else "general",
            max_results=5,
            exclude_domains=EXCLUDE_DOMAINS,
            include_raw=True,
        )
    except Exception as e:
        return [{"error": f"search failed: {e}"}]

    cleaned = []
    for r in results:
        content = r.get("content") or ""
        title = r.get("title") or ""
        if not content or len(content) < 50:
            continue
        if is_noise(content) or is_chrome(content):
            continue
        if _is_garbage_title(title) and len(content) < 200:
            continue
        if r.get("score", 0) < min_score:
            continue
        if not is_new(content, memory):
            continue
        cleaned.append({
            "title": title[:120],
            "snippet": (r.get("snippet") or content[:300]),
            "url": r.get("url", ""),
            "score": round(r.get("score", 0), 2),
        })
    return cleaned


def _record(args: dict, competitor_names: set[str], memory: dict) -> dict | None:
    """Validate and normalise a record_finding tool call into a findings dict.
    Returns None (and a reason to surface to the model) if invalid."""
    comp = (args.get("competitor") or "").strip()
    content = (args.get("content") or "").strip()
    if not comp or not content:
        return None
    if comp not in competitor_names:
        # Don't let the model invent a new competitor — it would break
        # downstream config lookups in analyzer._update_memory_from_analysis.
        return None
    if not is_new(content, memory):
        return None
    mark_seen(content, memory)
    topic = (args.get("topic") or "deepened").strip()[:60]
    return {
        "competitor": comp,
        "source": "deepen",
        "topic": topic,
        "content": content,
        "snippet": content[:300],
        "title": (args.get("title") or "").strip()[:200],
        "url": (args.get("url") or "").strip(),
        "relevance": 0.6,  # model-vetted — assume mid-high signal
        "search_provider": "deepen",
        "published": "",
        "rationale": (args.get("rationale") or "").strip()[:300],
    }


def _empty_trace(status: str, **extra) -> dict:
    """Trace skeleton for early-exit paths — keeps callers' rendering code simple."""
    return {
        "status": status,           # ran | disabled | empty_findings | zero_budget | error
        "max_searches": 0,
        "max_records": 0,
        "searches": [],             # [{query, topic, results}]
        "records": [],              # [{competitor, topic, title, url, rationale}]
        "rejected_records": 0,
        "stopped_early": False,
        "error": None,
        **extra,
    }


def deepen_findings(findings: list[dict], config: dict, memory: dict) -> tuple[list[dict], dict]:
    """Run one agentic follow-up pass.

    Returns (findings_with_new_items, trace). The trace is always populated —
    even on the no-op paths — so callers can render a uniform 'Agent activity'
    section without special-casing disabled/empty runs. Fails soft: on any
    error the trace status becomes 'error' and findings pass through unchanged.
    """

    if os.environ.get("DEEPEN_ENABLED", "1") not in ("1", "true", "True"):
        return findings, _empty_trace("disabled")
    if not findings:
        # Nothing for the model to chase — skip the call entirely to save tokens.
        return findings, _empty_trace("empty_findings")

    max_searches = _budget("DEEPEN_MAX_SEARCHES", 3)
    max_new = _budget("DEEPEN_MAX_NEW_FINDINGS", 6)
    if max_searches == 0 or max_new == 0:
        return findings, _empty_trace("zero_budget",
                                       max_searches=max_searches,
                                       max_records=max_new)

    competitor_names = {c["name"] for c in config.get("competitors", [])}
    company = config.get("company", "the company")

    threat_lines = []
    for c in config.get("competitors", []):
        if c.get("_threat_angle"):
            threat_lines.append(f"- {c['name']}: {c['_threat_angle']}")
    threat_block = "\n".join(threat_lines) if threat_lines else "(none configured)"

    system = (
        f"You are a competitive-intelligence research assistant for {company}. "
        "A scripted scan has already run and produced today's findings "
        "(shown below). Your job is to chase ONE OR TWO loose threads the "
        "scripted scan couldn't follow up on — a rumored deal, a vague "
        "hiring signal, a product leak that deserves confirmation.\n\n"
        f"Budget: at most {max_searches} search_web calls and "
        f"{max_new} record_finding calls TOTAL (not per-competitor). "
        "Be frugal. Call `stop` as soon as you've either (a) confirmed a lead "
        "worth recording, or (b) decided the leads aren't worth chasing.\n\n"
        "Rules:\n"
        "- Do NOT restate broad queries the scripted scan already covers "
        '(e.g. "<competitor> product launch"). Go narrow: site:techcrunch.com, '
        'exact phrases, specific people or products.\n'
        "- Only record_finding on content that materially adds to what's "
        "already in the findings — a new angle, a confirmation, a detail.\n"
        "- If the scripted findings are quiet or already comprehensive, "
        "call `stop` immediately with zero searches. That's a valid outcome.\n\n"
        "## Competitor threat angles\n"
        f"{threat_block}"
    )

    user_prompt = (
        "Scripted scan findings (already captured — DO NOT re-record these):\n"
        f"{_summarise_findings(findings)}\n\n"
        "Review the findings, identify at most one or two loose threads "
        "worth chasing, run your searches, record any material new "
        "finding(s), then call `stop`."
    )

    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    new_findings: list[dict] = []
    searches_used = 0
    trace = _empty_trace("ran", max_searches=max_searches, max_records=max_new)

    try:
        for _turn in range(max_searches + max_new + 2):  # hard outer bound
            resp = _client.messages.create(
                model=MODEL,
                max_tokens=1500,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                break

            tool_results = []
            stopped = False
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue

                if block.name == "stop":
                    stopped = True
                    trace["stopped_early"] = searches_used < max_searches
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "stopped",
                    })
                    continue

                if block.name == "search_web":
                    if searches_used >= max_searches:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "BUDGET EXHAUSTED — no more searches allowed. Call `stop`.",
                        })
                        continue
                    query = (block.input or {}).get("query", "")
                    topic = (block.input or {}).get("topic", "general")
                    results = _run_search(query, topic, memory)
                    searches_used += 1
                    trace["searches"].append({
                        "query": query, "topic": topic, "results": len(results),
                    })
                    print(f"  [deepen] search #{searches_used}: {query!r} -> {len(results)} results")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(results)[:8000],
                    })
                    continue

                if block.name == "record_finding":
                    if len(new_findings) >= max_new:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "BUDGET EXHAUSTED — already recorded max findings. Call `stop`.",
                        })
                        continue
                    finding = _record(block.input or {}, competitor_names, memory)
                    if finding is None:
                        trace["rejected_records"] += 1
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "rejected (duplicate, unknown competitor, or empty content)",
                        })
                        continue
                    new_findings.append(finding)
                    trace["records"].append({
                        "competitor": finding["competitor"],
                        "topic": finding["topic"],
                        "title": finding.get("title", ""),
                        "url": finding.get("url", ""),
                        "rationale": finding.get("rationale", ""),
                    })
                    print(f"  [deepen] recorded: {finding['competitor']} / {finding['topic']}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "recorded",
                    })
                    continue

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"unknown tool: {block.name}",
                    "is_error": True,
                })

            messages.append({"role": "user", "content": tool_results})
            if stopped:
                break
    except Exception as e:
        print(f"  [deepen] agentic pass failed (non-fatal): {e}")
        trace["status"] = "error"
        trace["error"] = str(e)[:300]
        return findings, trace

    if new_findings:
        print(f"  [deepen] added {len(new_findings)} finding(s) via {searches_used} search(es)")
    else:
        print(f"  [deepen] no new findings recorded ({searches_used} search(es) used)")

    return findings + new_findings, trace


def render_trace_markdown(trace: dict) -> str:
    """Render a deepen trace as a markdown section for the report.

    Empty/no-op runs still get a one-line status so the reader always knows
    whether the agentic layer ran and what it cost."""
    status = trace.get("status", "unknown")

    if status == "disabled":
        return "## Agent Activity (deepen pass)\n\n_Disabled this run (DEEPEN_ENABLED=0)._\n"
    if status == "empty_findings":
        return "## Agent Activity (deepen pass)\n\n_Skipped — scripted scan returned zero findings, nothing to chase._\n"
    if status == "zero_budget":
        return "## Agent Activity (deepen pass)\n\n_Skipped — search or record budget is set to 0._\n"

    lines = ["## Agent Activity (deepen pass)", ""]
    searches = trace.get("searches", [])
    records = trace.get("records", [])
    rejected = trace.get("rejected_records", 0)
    max_s = trace.get("max_searches", 0)
    max_r = trace.get("max_records", 0)
    stopped_early = trace.get("stopped_early", False)

    lines.append(
        f"Ran **{len(searches)} of {max_s}** allowed searches, "
        f"recorded **{len(records)} of {max_r}** allowed findings"
        + (f", rejected **{rejected}** invalid record attempts" if rejected else "")
        + "."
    )
    if stopped_early:
        lines.append("")
        lines.append("_Model called `stop` before using the full search budget — it judged further searching unlikely to help._")

    if status == "error":
        lines.append("")
        lines.append(f"**⚠ Errored partway through:** {trace.get('error')}")

    if searches:
        lines.append("")
        lines.append("**Searches the agent chose to run:**")
        for i, s in enumerate(searches, 1):
            lines.append(f"{i}. `{s['query']}` ({s['topic']}) → {s['results']} usable result(s)")

    if records:
        lines.append("")
        lines.append("**What it promoted into today's findings:**")
        for r in records:
            head = f"- **{r['competitor']}** / {r['topic']}"
            if r.get("title"):
                head += f" — {r['title']}"
            lines.append(head)
            if r.get("rationale"):
                lines.append(f"  - _why:_ {r['rationale']}")
            if r.get("url"):
                lines.append(f"  - {r['url']}")
    elif searches:
        lines.append("")
        lines.append("_Nothing from the searches was promoted — the agent judged the results not material enough to record._")

    return "\n".join(lines) + "\n"
