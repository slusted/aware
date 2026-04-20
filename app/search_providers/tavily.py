"""Tavily adapter — owns the Tavily HTTP call, the API key + freshness
module state, and the `SearchProvider` protocol wrapper.

Previously this was a thin shim that called back into scanner.search_tavily;
scanner is now the consumer and this module is the source of truth.

The legacy `scanner.search_tavily` name is preserved as a re-export
(see scanner.py) because `app/search_providers/__init__.py:install_scanner_hook`
monkey-patches that attribute to layer fan-out + enrichment on top — keeping
a single chokepoint. Raw (unhooked) Tavily is callable via
`TavilyProvider.search_news` or `app.search_providers.tavily.search_tavily`
directly.
"""
from __future__ import annotations

import json
import os
import urllib.request

from app.adapters.fetch.sanitize import (
    RAW_CONTENT_MAX,
    clean_extracted,
    is_bot_wall,
    is_chrome,
)
from .base import SearchProvider


TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# Module-level freshness default. Scan orchestration (app/jobs.py) sets this
# in this module before each scan so every search only returns content from
# the last N days. None = no restriction (full index).
TAVILY_DAYS: int | None = None


def _apply_freshness(body: dict, days: int | None):
    """Attach freshness filters to a Tavily request body. Tavily supports
    `days` directly for topic=news, and `time_range` (day/week/month/year)
    for topic=general — we set both so it works either way."""
    if not days:
        return
    body["days"] = max(1, int(days))
    if days <= 1:
        body["time_range"] = "day"
    elif days <= 7:
        body["time_range"] = "week"
    elif days <= 31:
        body["time_range"] = "month"
    else:
        body["time_range"] = "year"


def search_tavily(
    query: str,
    search_depth: str = "advanced",
    topic: str = "general",
    max_results: int = 5,
    exclude_domains: list[str] = None,
    include_domains: list[str] = None,
    include_raw: bool = True,
    days: int | None = None,
) -> list[dict]:
    """
    Search via Tavily API. Returns list of {title, content, url, score, raw_content}.
    search_depth: "basic" (fast), "advanced" (extracts relevant page sections)
    topic: "general" or "news"
    include_raw: if True, returns cleaned page content (markdown) in raw_content
    include_domains: limit results to ONLY these domains (e.g., newsrooms)
    exclude_domains: exclude these domains from results
    """
    if not TAVILY_API_KEY:
        return []

    body = {
        "query": query,
        "search_depth": search_depth,
        "topic": topic,
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": "markdown" if include_raw else False,
    }
    if exclude_domains:
        body["exclude_domains"] = exclude_domains
    if include_domains:
        body["include_domains"] = include_domains
    _apply_freshness(body, days if days is not None else TAVILY_DAYS)

    payload = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TAVILY_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = []
        for r in data.get("results", []):
            # Use raw_content if available (much richer), fall back to snippet.
            # Strip image/nav boilerplate BEFORE truncating so we spend the
            # char budget on actual article text.
            raw = clean_extracted(r.get("raw_content", ""))
            content = r.get("content", "")
            # Reject Cloudflare challenge pages etc. — Tavily's own scraper
            # falls into the same traps we do; if raw is a bot-wall page,
            # drop it so we fall back to the snippet.
            if raw and (is_bot_wall(raw) or is_chrome(raw)):
                raw = ""
            # Tavily already truncates on their side; RAW_CONTENT_MAX is just a
            # safety bound against pathologically large pages.
            if raw and len(raw) > RAW_CONTENT_MAX:
                raw = raw[:RAW_CONTENT_MAX]
            results.append({
                "title": r.get("title", ""),
                "content": raw if raw else content,
                "snippet": content,
                "url": r.get("url", ""),
                "score": r.get("score", 0),
                "source_provider": "tavily",
                "published": r.get("published_date", ""),
            })
        try:
            from app.usage import record_tavily
            record_tavily(depth=search_depth, topic=topic, result_count=len(results), success=True)
        except Exception:
            pass
        return results
    except Exception as e:
        detail = ""
        try:
            if hasattr(e, "read"):
                detail = ": " + e.read().decode("utf-8", errors="ignore")[:200]
        except Exception:
            pass
        print(f"  [tavily] Error: {e}{detail}")
        try:
            from app.usage import record_tavily
            record_tavily(depth=search_depth, topic=topic, result_count=0, success=False)
        except Exception:
            pass
        return []


class TavilyProvider(SearchProvider):
    name = "tavily"
    description = "Tavily — AI-native web search with raw page extraction. Primary engine."
    env_var = "TAVILY_API_KEY"

    @classmethod
    def available(cls) -> bool:
        return bool(os.environ.get(cls.env_var, ""))

    def search_news(self, query, *, max_results=5, days=None):
        results = search_tavily(
            query, search_depth="advanced", topic="news",
            max_results=max_results, days=days,
        )
        return [{**r, "source_provider": "tavily"} for r in results]
