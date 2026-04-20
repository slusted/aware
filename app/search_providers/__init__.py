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

REGISTRY: dict[str, type[SearchProvider]] = {
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
    """Wrap scanner.search_tavily with two behaviors:
      1. News queries (topic='news') fan out to every other news-scope provider
         (Serper today) and merge by URL.
      2. Every result — Tavily or fan-out — with a short body gets its URL
         fetched through app.fetcher (ZenRows → ScrapingBee → trafilatura)
         so we store full page text, not just snippets."""
    global _hook_installed
    if _hook_installed:
        return
    import scanner
    original = scanner.search_tavily

    def wrapped(query, *args, **kwargs):
        topic = kwargs.get("topic", "general")
        if len(args) >= 2:
            topic = args[1]
        results = original(query, *args, **kwargs)

        # News-topic fan-out to Serper etc.
        extras: list[dict] = []
        if topic == "news":
            max_results = kwargs.get("max_results", 5)
            from . import tavily as _tavily
            days = kwargs.get("days") or getattr(_tavily, "TAVILY_DAYS", None)
            for p in news_providers():
                if getattr(p, "name", None) == "tavily":
                    continue
                try:
                    extras.extend(p.search_news(query, max_results=max_results, days=days))
                except Exception as e:
                    print(f"[providers/{p.name}] error: {e}")

        # Enrichment runs over every Tavily result AND fan-out extra: we want
        # ZenRows' extraction on every URL, not just thin-body ones, because
        # Tavily's raw_content is inconsistent and ZenRows is the canonical
        # content pipeline. Only the score threshold and the 20-URL hard cap
        # gate this. SKIP_DOMAINS are honored inside fetcher.
        if _enrichment_enabled():
            try:
                min_score = scanner.current_min_relevance()
            except Exception:
                min_score = 0.0
            pool = results + extras
            seen_urls: set[str] = set()
            urls_to_enrich: list[str] = []
            for r in pool:
                url = r.get("url") or ""
                if not url or url in seen_urls:
                    continue
                if float(r.get("score", 0) or 0) < min_score:
                    continue
                urls_to_enrich.append(url)
                seen_urls.add(url)
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
                    # search provider supplied — Serper's "date" is often a
                    # crawl date or "N days ago" fuzz, and Tavily occasionally
                    # disagrees with the article's own metadata. The HTML
                    # tells the truth. Falls through (keeps provider date) if
                    # we couldn't extract a page date.
                    page_date = fetcher.get_page_date(url)
                    if page_date:
                        r["published"] = page_date

        if not extras:
            return results

        seen = {r.get("url") for r in results if r.get("url")}
        for e in extras:
            u = e.get("url")
            if u and u in seen:
                continue
            if u:
                seen.add(u)
            results.append(e)
        return results

    scanner.search_tavily = wrapped
    _hook_installed = True
    print("[providers] scanner hook installed (fan-out + enrichment)")
