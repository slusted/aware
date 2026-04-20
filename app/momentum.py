"""Momentum tracking — daily time-series signals for each competitor.

Three data sources, all free:

  1. Google Trends (pytrends)           → "google_trends" metric (0-100 interest)
  2. iOS App Store top-free list        → "ios_rank" (1-200, or null if not in top 200)
  3. Google Play scraper                → "play_installs", "play_rating", "play_reviews"

Each fetcher is isolated: one competitor failing (rate-limited, broken app id, etc)
does not stop the others. Results are UPSERTed by (competitor, metric, YYYY-MM-DD)
so re-running the same day overwrites rather than duplicates.

The job is registered in app.scheduler and runs once per day shortly before the
main scan. It writes CompetitorMetric rows; the UI reads them on the competitor
profile page.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import select

from .db import SessionLocal
from .models import Competitor, CompetitorMetric


# ───────────────────────────────────────────────────────────────
#  Storage — upsert by (competitor_id, metric, YYYY-MM-DD)
# ───────────────────────────────────────────────────────────────

def _today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def record_metric(
    db: Session,
    competitor_id: int,
    metric: str,
    value: float | None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Upsert one metric value for today. value=None is a legitimate record —
    it means "we tried, but the signal wasn't available" (e.g., app not in top 200).
    Distinguishes from "we never tried", which is no row at all."""
    today = _today_str()
    existing = (
        db.query(CompetitorMetric)
        .filter(
            CompetitorMetric.competitor_id == competitor_id,
            CompetitorMetric.metric == metric,
            CompetitorMetric.collected_date == today,
        )
        .one_or_none()
    )
    if existing:
        existing.value = value
        existing.meta = meta or {}
        existing.collected_at = datetime.utcnow()
    else:
        db.add(CompetitorMetric(
            competitor_id=competitor_id,
            metric=metric,
            value=value,
            meta=meta or {},
            collected_date=today,
            collected_at=datetime.utcnow(),
        ))
    db.commit()


# ───────────────────────────────────────────────────────────────
#  1) Google Trends — pytrends
# ───────────────────────────────────────────────────────────────

_PYTRENDS_CLIENT = None


def _patch_urllib3_retry_for_pytrends() -> None:
    """pytrends 4.9 passes `method_whitelist` to urllib3.Retry, which was removed
    in urllib3 v2.0 in favor of `allowed_methods`. Alias it so pytrends keeps
    working without pinning urllib3 to <2. One-shot, idempotent."""
    try:
        from urllib3.util.retry import Retry
    except ImportError:
        return
    if getattr(Retry, "_cw_patched", False):
        return
    original_init = Retry.__init__

    def patched_init(self, *args, **kwargs):
        if "method_whitelist" in kwargs and "allowed_methods" not in kwargs:
            kwargs["allowed_methods"] = kwargs.pop("method_whitelist")
        return original_init(self, *args, **kwargs)

    Retry.__init__ = patched_init
    Retry._cw_patched = True


def _get_pytrends():
    """Lazy-init pytrends. Returns None if the library isn't installed so the
    rest of the job can still run."""
    global _PYTRENDS_CLIENT
    if _PYTRENDS_CLIENT is not None:
        return _PYTRENDS_CLIENT
    _patch_urllib3_retry_for_pytrends()
    try:
        from pytrends.request import TrendReq
    except ImportError:
        print("  [momentum] pytrends not installed — skipping Google Trends")
        return None
    # hl=en-AU biases to Australian English; tz is minutes west of UTC.
    _PYTRENDS_CLIENT = TrendReq(hl="en-AU", tz=0, timeout=(5, 15), retries=2, backoff_factor=0.5)
    return _PYTRENDS_CLIENT


def fetch_google_trends(keyword: str, geo: str = "AU") -> tuple[float | None, dict]:
    """Return (interest_score_0_to_100, meta). Reads the most recent weekly value
    from the last 30 days. Returns (None, {"error": ...}) on failure."""
    client = _get_pytrends()
    if client is None:
        return None, {"error": "pytrends unavailable"}
    try:
        client.build_payload([keyword], timeframe="today 1-m", geo=geo, gprop="")
        df = client.interest_over_time()
        if df is None or df.empty or keyword not in df.columns:
            return None, {"keyword": keyword, "geo": geo, "note": "no data"}
        # Last row = most recent observation
        latest = float(df[keyword].iloc[-1])
        return latest, {"keyword": keyword, "geo": geo, "timeframe": "today 1-m"}
    except Exception as e:
        return None, {"keyword": keyword, "geo": geo, "error": str(e)[:200]}


# ───────────────────────────────────────────────────────────────
#  2) iOS App Store rank — Apple Marketing Tools RSS
# ───────────────────────────────────────────────────────────────

# Apple's Marketing Tools RSS returns the top N free apps. The endpoint caps
# out around 100 per region — 200 often 500s — so we default to 100. We cache
# the list per (country, limit) so each competitor lookup after the first is
# a local search, not another HTTP round-trip.
_IOS_TOP_CACHE: dict[tuple[str, int], list[dict]] = {}


def _ios_top_free(country: str = "au", limit: int = 100) -> list[dict]:
    """Fetch + cache the top-free apps list for this country. Called repeatedly
    but the HTTP call only happens once per (country, limit) per process run."""
    key = (country, limit)
    if key in _IOS_TOP_CACHE:
        return _IOS_TOP_CACHE[key]
    url = f"https://rss.applemarketingtools.com/api/v2/{country}/apps/top-free/{limit}/apps.json"
    req = urllib.request.Request(url, headers={"User-Agent": "competitor-watch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("feed", {}).get("results", []) or []
    except Exception as e:
        print(f"  [momentum] iOS top-free fetch failed ({country}): {e}")
        results = []
    _IOS_TOP_CACHE[key] = results
    return results


def fetch_ios_rank(app_store_id: str, country: str = "au") -> tuple[float | None, dict]:
    """Find the competitor's app in the top-200 list. Returns (rank, meta)
    where rank is 1-based position (1=top), or (None, meta) if not in top 200."""
    if not app_store_id:
        return None, {"error": "no app_store_id"}
    results = _ios_top_free(country=country, limit=100)
    if not results:
        return None, {"country": country, "error": "top list unavailable"}
    app_store_id = str(app_store_id).strip()
    for idx, entry in enumerate(results, start=1):
        if str(entry.get("id", "")) == app_store_id:
            return float(idx), {
                "country": country,
                "app_store_id": app_store_id,
                "name": entry.get("name", ""),
                "list_size": len(results),
            }
    return None, {
        "country": country,
        "app_store_id": app_store_id,
        "note": f"not in top {len(results)}",
        "list_size": len(results),
    }


# ───────────────────────────────────────────────────────────────
#  3) Google Play — google-play-scraper
# ───────────────────────────────────────────────────────────────

def fetch_play_data(package: str, country: str = "au", lang: str = "en") -> dict[str, tuple[float | None, dict]]:
    """Returns a dict of {metric_name: (value, meta)} covering:
      - play_installs  (min installs, log-scale bucket lower bound)
      - play_rating    (average star rating, 0-5)
      - play_reviews   (total reviews count)
    Each entry has its own meta so per-metric failures are independent."""
    base_meta = {"package": package, "country": country, "lang": lang}
    try:
        from google_play_scraper import app as gp_app
    except ImportError:
        err = {**base_meta, "error": "google-play-scraper not installed"}
        return {
            "play_installs": (None, err),
            "play_rating": (None, err),
            "play_reviews": (None, err),
        }
    if not package:
        err = {**base_meta, "error": "no play_package"}
        return {
            "play_installs": (None, err),
            "play_rating": (None, err),
            "play_reviews": (None, err),
        }
    try:
        data = gp_app(package, lang=lang, country=country)
    except Exception as e:
        err = {**base_meta, "error": str(e)[:200]}
        return {
            "play_installs": (None, err),
            "play_rating": (None, err),
            "play_reviews": (None, err),
        }
    installs = data.get("minInstalls") or data.get("realInstalls")
    rating = data.get("score")
    reviews = data.get("ratings") or data.get("reviews")
    title = data.get("title", "")
    meta = {**base_meta, "title": title, "installs_bucket": data.get("installs", "")}
    return {
        "play_installs": (float(installs) if installs is not None else None, meta),
        "play_rating": (float(rating) if rating is not None else None, meta),
        "play_reviews": (float(reviews) if reviews is not None else None, meta),
    }


# ───────────────────────────────────────────────────────────────
#  Orchestration — called by the scheduler once per day
# ───────────────────────────────────────────────────────────────

# pytrends hits Google unofficially; back off between requests to avoid 429s.
_TRENDS_DELAY_SEC = 2.0


def run_momentum_for_competitor(db: Session, c: Competitor, country: str = "au") -> dict:
    """Collect all three signals for one competitor. Returns a summary of what
    was recorded (useful for logging)."""
    recorded: dict[str, Any] = {"competitor": c.name}

    # Google Trends — always on (every competitor has a name to search)
    keyword = (c.trends_keyword or c.name or "").strip()
    if keyword:
        value, meta = fetch_google_trends(keyword, geo=country.upper())
        record_metric(db, c.id, "google_trends", value, meta)
        recorded["google_trends"] = value
        time.sleep(_TRENDS_DELAY_SEC)  # be nice to Google
    else:
        recorded["google_trends"] = "skipped: no keyword"

    # iOS rank
    if c.app_store_id:
        value, meta = fetch_ios_rank(c.app_store_id, country=country)
        record_metric(db, c.id, "ios_rank", value, meta)
        recorded["ios_rank"] = value
    else:
        recorded["ios_rank"] = "skipped: no app_store_id"

    # Google Play bundle
    if c.play_package:
        play = fetch_play_data(c.play_package, country=country)
        for metric, (value, meta) in play.items():
            record_metric(db, c.id, metric, value, meta)
            recorded[metric] = value
    else:
        recorded["play"] = "skipped: no play_package"

    return recorded


def run_momentum_job(country: str = "au") -> dict:
    """Top-level entry point. Iterates all active competitors and collects
    momentum signals for each. Per-competitor failures are isolated — one bad
    entry doesn't halt the run."""
    print(f"  [momentum] Starting momentum collection (country={country})")
    # Reset the per-process iOS cache so we get a fresh snapshot each run
    _IOS_TOP_CACHE.clear()

    db = SessionLocal()
    summary = {"competitors": [], "errors": []}
    try:
        competitors = db.query(Competitor).filter(Competitor.active == True).order_by(Competitor.name).all()
        for c in competitors:
            try:
                record = run_momentum_for_competitor(db, c, country=country)
                summary["competitors"].append(record)
                print(f"  [momentum] {c.name}: trends={record.get('google_trends')} "
                      f"ios={record.get('ios_rank')} play_installs={record.get('play_installs')}")
            except Exception as e:
                msg = f"{c.name}: {e}"
                summary["errors"].append(msg)
                print(f"  [momentum] ERROR {msg}")
    finally:
        db.close()
    print(f"  [momentum] Done. {len(summary['competitors'])} competitors, {len(summary['errors'])} errors")
    return summary


if __name__ == "__main__":
    # Allow manual invocation: `python -m app.momentum`
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    result = run_momentum_job(country=os.environ.get("MOMENTUM_COUNTRY", "au"))
    print(json.dumps(result, indent=2, default=str))
