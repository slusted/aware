"""Brave Search adapter — independent news index via Brave's Data for AI API.
Flat pricing (~$5/1000 queries on the standard tier at time of writing); usage
tracked per call. Enabled + listed first in config so Brave leads the fan-out
and wins URL dedup on overlap with Tavily/Serper."""
from __future__ import annotations
import json
import os
import sys
import urllib.parse
import urllib.request

from .base import SearchProvider
from .serper import _parse_age_days  # reuse the fuzzy-date parser


def _freshness(days: int | None) -> str | None:
    """Map a days window to Brave's `freshness` value.
    pd = past day, pw = past week, pm = past month, py = past year.
    None = no freshness restriction."""
    if not days:
        return None
    if days <= 1:
        return "pd"
    if days <= 7:
        return "pw"
    if days <= 31:
        return "pm"
    return "py"


class BraveProvider(SearchProvider):
    name = "brave"
    description = "Brave Search — independent news index (not a Google reseller). Primary news provider; leads fan-out and wins URL dedup."
    env_var = "BRAVE_API_KEY"

    _MAX_COUNT = 20  # Brave caps count at 20 per request for news

    @classmethod
    def available(cls) -> bool:
        return bool(os.environ.get(cls.env_var, ""))

    def search_news(self, query, *, max_results=5, days=None):
        key = os.environ.get(self.env_var, "")
        if not key:
            return []

        params = {
            "q": query,
            "count": min(max_results, self._MAX_COUNT),
            "country": os.environ.get("BRAVE_COUNTRY", "ALL"),
            "search_lang": os.environ.get("BRAVE_LANG", "en"),
            "safesearch": "moderate",
            "spellcheck": "false",
        }
        fresh = _freshness(days)
        if fresh:
            params["freshness"] = fresh

        url = "https://api.search.brave.com/res/v1/news/search?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "X-Subscription-Token": key,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self._record_usage(success=False)
            detail = ""
            try:
                if hasattr(e, "read"):
                    detail = ": " + e.read().decode("utf-8", errors="ignore")[:200]
            except Exception:
                pass
            print(f"  [brave] Error: {e}{detail}")
            return []

        # Brave has iterated the /news/search response shape — defensive pick:
        # top-level `results` on the news endpoint today, with a `news.results`
        # fallback in case we hit a web-search-with-news-inline response.
        raw_results = (
            data.get("results")
            or (data.get("news") or {}).get("results")
            or []
        )
        self._record_usage(success=True, result_count=len(raw_results))

        out = []
        stale_dropped = 0
        for r in raw_results:
            title = r.get("title") or ""
            snippet = r.get("description") or ""
            href = r.get("url") or ""
            if not href:
                continue

            meta = r.get("meta_url") or {}
            page_age = r.get("page_age") or meta.get("page_age") or ""
            age_str = r.get("age") or ""
            age_days = _parse_age_days(age_str) if age_str else None
            published_display = page_age or age_str

            # Hard cutoff: drop anything older than the requested window,
            # matching the stale-drop behavior wired for Serper. If age is
            # unparseable, keep it (can't tell — better to include).
            if days is not None and age_days is not None and age_days > int(days):
                stale_dropped += 1
                continue

            out.append({
                "title": title,
                "content": snippet,
                "snippet": snippet,
                "url": href,
                "score": 0.5,  # Brave doesn't score news results
                "source_provider": "brave",
                "source_name": meta.get("hostname") or "",
                "published": published_display,
                "age_days": age_days,
            })

        if stale_dropped:
            try:
                sys.__stdout__.write(
                    f"[brave] dropped {stale_dropped}/{len(raw_results)} stale "
                    f"(older than {days}d) — freshness={fresh or 'none'}\n"
                )
            except Exception:
                pass
        return out

    def _record_usage(self, *, success: bool, result_count: int = 0):
        """Log one Brave call to usage_events. Flat per-call pricing
        (standard Data for AI tier ≈ $0.005/call)."""
        try:
            from ..db import SessionLocal
            from ..models import UsageEvent
            from ..usage import current_run_id
            rate = float(os.environ.get("BRAVE_USD_PER_CALL", "0.005"))
            db = SessionLocal()
            try:
                db.add(UsageEvent(
                    run_id=current_run_id.get(),
                    provider="brave",
                    operation="news",
                    model="brave/news",
                    credits=1,
                    cost_usd=rate if success else 0.0,
                    success=success,
                    extra={"result_count": result_count},
                ))
                db.commit()
            finally:
                db.close()
        except Exception:
            pass
