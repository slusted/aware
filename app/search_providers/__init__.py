"""Pluggable search providers.

Tavily is the primary engine (page-content extraction, deep search). Other
providers — currently Serper (Google News) — can be enabled in the
search_providers block of config.json and layer on top for specific scopes
(e.g., news). The scanner's news queries fan out to every active provider and
merge results with URL dedup.

Adding a new provider:
  1. Subclass SearchProvider in a new module
  2. Register it in REGISTRY below
  3. Add a block under "search_providers" in config.json

At startup, install_scanner_hook() is called once to monkey-patch
scanner.search_tavily so news queries pick up all active news-scope providers
without any scanner.py edits.
"""
from __future__ import annotations
import os
from typing import Protocol

from .base import SearchProvider
from .tavily import TavilyProvider
from .serper import SerperProvider
from .brave import BraveProvider

REGISTRY: dict[str, type[SearchProvider]] = {
    "brave": BraveProvider,
    "tavily": TavilyProvider,
    "serper": SerperProvider,
}

# Populated by load_from_config() — list of (instance, scope_set)
_active: list[tuple[SearchProvider, set[str]]] = []
_hook_installed = False
_enrich_urls = True  # default ON; flipped by load_from_config()


def _enrichment_enabled() -> bool:
    return _enrich_urls


def load_from_config(config: dict):
    """Rebuild the active-providers list from the search_providers block.
    Called at startup and anytime the block changes via the settings UI."""
    global _enrich_urls
    block = (config or {}).get("search_providers") or {}
    _active.clear()
    for name, opts in block.items():
        if not isinstance(opts, dict) or not opts.get("enabled"):
            continue
        cls = REGISTRY.get(name)
        if not cls:
            print(f"[providers] unknown provider '{name}' — skipping")
            continue
        if not cls.available():
            print(f"[providers] '{name}' missing API key — disabled")
            continue
        scope = set(opts.get("scope", ["news"]))
        _active.append((cls(), scope))
        print(f"[providers] enabled '{name}' for scope={sorted(scope)}")

    # Top-level enrichment flag: serper/other snippet-only providers get their
    # URLs fetched+cleaned via Tavily /extract before being returned.
    _enrich_urls = bool((config or {}).get("enrich_provider_urls", True))
    print(f"[providers] URL enrichment: {'on' if _enrich_urls else 'off'}")


def news_providers() -> list[SearchProvider]:
    return [p for p, scope in _active if "news" in scope]


def provider_status(config: dict) -> list[dict]:
    """Shape for /settings/providers — one row per registered provider."""
    block = (config or {}).get("search_providers") or {}
    out = []
    for name, cls in REGISTRY.items():
        opts = block.get(name) or {}
        out.append({
            "name": name,
            "description": cls.description,
            "env_var": cls.env_var,
            "env_present": bool(os.environ.get(cls.env_var, "")),
            "enabled": bool(opts.get("enabled", False)),
            "scope": opts.get("scope", ["news"]),
            "available": cls.available(),
        })
    return out


def install_scanner_hook():
    """Wrap scanner.search_tavily with three behaviors:
      1. News queries (topic='news') fan out to every news-scope provider in
         config order. The first provider in the block leads the merged pool
         and wins URL dedup on overlap. Tavily no longer has a hardcoded
         first-position privilege — its ranking is whatever config says.
      2. Non-news topics (topic='general', etc.) bypass the fan-out and go
         Tavily-only, preserving the deep-search / brief call path.
      3. Every result — regardless of provider — gets its URL fetched through
         app.fetcher (ZenRows → ScrapingBee → trafilatura) so we store full
         page text, not just snippets."""
    global _hook_installed
    if _hook_installed:
        return
    import scanner
    original = scanner.search_tavily

    def wrapped(query, *args, **kwargs):
        topic = kwargs.get("topic", "general")
        if len(args) >= 2:
            topic = args[1]

        if topic != "news":
            # Non-news goes Tavily-only; enrichment below still runs.
            buckets: dict[str, list[dict]] = {"tavily": original(query, *args, **kwargs)}
            ordered = ["tavily"]
        else:
            max_results = kwargs.get("max_results", 5)
            from . import tavily as _tavily
            days = kwargs.get("days") or getattr(_tavily, "TAVILY_DAYS", None)
            ordered = [p.name for p in news_providers()]
            buckets = {}
            for p in news_providers():
                try:
                    if p.name == "tavily":
                        # Preserve the original call path (raw_content,
                        # Tavily's own usage accounting, kwargs honoring).
                        buckets[p.name] = original(query, *args, **kwargs)
                    else:
                        buckets[p.name] = p.search_news(
                            query, max_results=max_results, days=days,
                        )
                except Exception as e:
                    print(f"[providers/{p.name}] error: {e}")
                    buckets[p.name] = []
            # Safety net: if Tavily isn't in news_providers config for some
            # reason, still call it so the scan doesn't go dark. Appended last.
            if "tavily" not in buckets:
                buckets["tavily"] = original(query, *args, **kwargs)
                ordered.append("tavily")

        # Build the pool in priority order with first-wins URL dedup. Whoever
        # is first in config owns the record (title, score, source_provider).
        pool: list[dict] = []
        seen_urls: set[str] = set()
        for name in ordered:
            for r in buckets.get(name, []):
                u = r.get("url") or ""
                if u and u in seen_urls:
                    continue
                if u:
                    seen_urls.add(u)
                pool.append(r)

        # Enrichment runs over every pool entry: ZenRows is the canonical
        # content pipeline; Tavily's raw_content is inconsistent. Score
        # threshold + 20-URL hard cap gate this. SKIP_DOMAINS honored in
        # fetcher.
        if _enrichment_enabled():
            try:
                min_score = scanner.current_min_relevance()
            except Exception:
                min_score = 0.0
            enrich_seen: set[str] = set()
            urls_to_enrich: list[str] = []
            for r in pool:
                url = r.get("url") or ""
                if not url or url in enrich_seen:
                    continue
                if float(r.get("score", 0) or 0) < min_score:
                    continue
                urls_to_enrich.append(url)
                enrich_seen.add(url)
            urls_to_enrich = urls_to_enrich[:20]
            if urls_to_enrich:
                try:
                    from .. import fetcher
                    fetched = fetcher.bulk_fetch(urls_to_enrich)
                except Exception as ex:
                    print(f"[providers] fetch enrichment failed: {ex}")
                    fetched = {}
                for r in pool:
                    url = r.get("url") or ""
                    res = fetched.get(url)
                    if not res:
                        continue
                    content, source = res
                    if content:
                        r["snippet"] = r.get("snippet") or r.get("content") or ""
                        r["content"] = content
                        r["enriched"] = True
                        r["enrichment_source"] = source
                    # Prefer the page's own published date over whatever the
                    # search provider supplied — provider dates are often
                    # crawl fuzz or disagree with article metadata.
                    page_date = fetcher.get_page_date(url)
                    if page_date:
                        r["published"] = page_date

        return pool

    scanner.search_tavily = wrapped
    _hook_installed = True
    print("[providers] scanner hook installed (priority fan-out + enrichment)")
