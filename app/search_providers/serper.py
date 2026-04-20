"""Serper.dev adapter — direct Google News via the Serper SERP API.
Pricing is ~$0.30 per 1000 queries; usage is tracked via record_tavily-like
accounting (we map credits=1 per news call). Enable in settings/providers."""
from __future__ import annotations
import json
import os
import re
import sys
import urllib.request

from .base import SearchProvider


def _qdr(days: int | None) -> str | None:
    """Map a days window to Google's tbs=qdr value. Note: this filters on
    crawl/index date, not published date — Google will still occasionally
    return stale content that was recently re-indexed. We layer a client-side
    age check (see _parse_age_days) on top."""
    if not days:
        return None
    if days <= 1:
        return "d"
    if days <= 7:
        return "w"
    if days <= 31:
        return "m"
    return "y"


_AGE_RE = re.compile(
    r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)


def _parse_age_days(date_str: str) -> int | None:
    """Parse Serper's fuzzy date ('3 days ago', '5 months ago', '1 year ago')
    into an approximate age in days. Returns 0 for sub-day ages.
    None = unparseable (keep the result, can't tell the age)."""
    if not date_str:
        return None
    m = _AGE_RE.search(date_str.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit in ("second", "minute", "hour"):
        return 0
    if unit == "day":
        return n
    if unit == "week":
        return n * 7
    if unit == "month":
        return n * 30
    if unit == "year":
        return n * 365
    return None


class SerperProvider(SearchProvider):
    name = "serper"
    description = "Serper.dev — direct Google News results. Use for recency + breadth beyond Tavily's index."
    env_var = "SERPER_API_KEY"

    @classmethod
    def available(cls) -> bool:
        return bool(os.environ.get(cls.env_var, ""))

    def search_news(self, query, *, max_results=5, days=None):
        key = os.environ.get(self.env_var, "")
        if not key:
            return []
        body = {"q": query, "num": max_results}
        qdr = _qdr(days)
        if qdr:
            body["tbs"] = f"qdr:{qdr}"
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            "https://google.serper.dev/news",
            data=payload,
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self._record_usage(success=False)
            raise

        raw_results = data.get("news", []) or []
        self._record_usage(success=True, result_count=len(raw_results))

        out = []
        stale_dropped = 0
        for r in raw_results:
            snippet = r.get("snippet") or ""
            date_str = r.get("date") or ""
            age_days = _parse_age_days(date_str)

            # Hard cutoff: if we asked for last N days and Serper/Google
            # returned something older (they sometimes do — qdr filters by
            # crawl date, not publish date), drop it. If age is unparseable,
            # keep the result (can't tell — better to include than miss).
            if days is not None and age_days is not None and age_days > int(days):
                stale_dropped += 1
                continue

            out.append({
                "title": r.get("title", ""),
                "content": snippet,
                "snippet": snippet,
                "url": r.get("link", ""),
                "score": 0.5,           # Serper doesn't score; middle-weight
                "source_provider": "serper",
                "source_name": r.get("source") or "",
                "published": date_str,  # keep the original string for display
                "age_days": age_days,   # parsed age for downstream filtering
            })

        if stale_dropped:
            try:
                sys.__stdout__.write(
                    f"[serper] dropped {stale_dropped}/{len(raw_results)} stale "
                    f"(older than {days}d) — qdr:{qdr or 'none'}\n"
                )
            except Exception:
                pass
        return out

    def _record_usage(self, *, success: bool, result_count: int = 0):
        """Log one Serper call to usage_events. Serper has flat pricing per
        call so we treat each call as 1 credit at the configured rate."""
        try:
            from ..db import SessionLocal
            from ..models import UsageEvent
            from ..usage import current_run_id
            rate = float(os.environ.get("SERPER_USD_PER_CALL", "0.0003"))
            db = SessionLocal()
            try:
                db.add(UsageEvent(
                    run_id=current_run_id.get(),
                    provider="serper",
                    operation="news",
                    model="serper/news",
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
