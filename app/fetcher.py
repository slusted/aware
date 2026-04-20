"""Fetch + extract article text from arbitrary URLs.

Pipeline per URL (adapts to what's configured):

  Reddit /comments/ URLs always take the JSON fast-path first — Reddit
  exposes every comments page as clean JSON at {url}.json with no auth,
  which is strictly better than scraping their React-rendered HTML.

  If ZENROWS_API_KEY is set (paid, preferred — primary by default):
    1. ZenRows premium proxy + trafilatura      ← primary
    2. ScrapingBee premium proxy + trafilatura  ← paid fallback
    3. urllib + trafilatura                     ← last-ditch free fallback

  Else if SCRAPINGBEE_API_KEY is set:
    1. ScrapingBee premium proxy + trafilatura  ← primary
    2. urllib + trafilatura                     ← last-ditch free fallback

  Otherwise (no key):
    1. urllib + trafilatura  ← primary (free)

Results are cached in-memory for the process lifetime so the same URL
surfaced by multiple competitor workers is fetched once.
"""
from __future__ import annotations
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from typing import Optional

from app.adapters.fetch.sanitize import clean_extracted, is_bot_wall, is_chrome, RAW_CONTENT_MAX

try:
    import trafilatura
    _HAS_TRAFILATURA = True
except Exception:
    trafilatura = None
    _HAS_TRAFILATURA = False


# Domains where fetching reliably returns junk (login walls, aggressive
# anti-scrape). Stay snippet-only for these — no point burning time.
#
# Split into two buckets because a naïve `domain in url` substring match is
# unsafe: "t.co" matched `reddi<t.co>m`, silently skipping every Reddit URL
# the scanner surfaced (including our target /comments/ threads).
#
#  SKIP_HOSTS: compared against the URL's *hostname* (suffix match, so
#              "x.com" also skips "www.x.com" but NOT "reddit.com").
#  SKIP_PATH_PREFIXES: compared against host+path as a substring — for
#              LinkedIn where only some paths are hopeless (posts/pulse/feed)
#              but the domain itself is fine for company pages.
SKIP_HOSTS = {
    "x.com", "twitter.com", "t.co",
    "facebook.com", "instagram.com",
    "medium.com",                      # metered paywall
}
SKIP_PATH_PREFIXES = (
    "linkedin.com/posts",
    "linkedin.com/pulse",
    "linkedin.com/feed",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Content-size caps to avoid OOM on accidentally-huge pages.
_HTTP_READ_CAP = 2_000_000   # 2 MB raw HTML
_MIN_GOOD_BODY = 500         # below this, fall through to the next tier

# Per-process cache. Keyed by URL. Protected by a lock because multiple
# competitor worker threads hit this concurrently.
_cache: dict[str, tuple[Optional[str], str]] = {}
# Page publication date, when we could extract one from the HTML's own
# metadata (<meta property="article:published_time">, OG tags, JSON-LD,
# etc.) or, for Reddit, from the post's created_utc. Keyed by URL; value
# is a plain 'YYYY-MM-DD' string so `_parse_published` picks it up without
# special handling. Populated as a side effect of a successful fetch;
# callers pull it via `get_page_date(url)` to override the search
# provider's date, which is often a crawl date or fuzzy "3 days ago".
_page_dates: dict[str, str] = {}
_cache_lock = threading.Lock()

# Fetcher mode. Toggled from /settings/providers via app.fetcher.configure().
# False = free tiers first, ScrapingBee only if all free tiers fail (cheap)
# True  = ScrapingBee first for every URL, free tiers as outage fallback
_scrapingbee_primary = False

# ZenRows is the preferred paid scraper when its key is present — defaults to
# primary so every URL goes through it first. Toggle off via /settings/providers
# to fall back to the ScrapingBee/free-tier cascade.
_zenrows_primary = True


def configure(cfg: dict):
    """Called at startup and whenever the providers settings change. Reads
    cfg['fetcher'] so UI toggles take effect without a server restart."""
    global _scrapingbee_primary, _zenrows_primary
    block = (cfg or {}).get("fetcher") or {}
    _scrapingbee_primary = bool(block.get("scrapingbee_primary", False))
    _zenrows_primary = bool(block.get("zenrows_primary", True))
    print(f"[fetcher] zenrows_primary = {_zenrows_primary} "
          f"scrapingbee_primary = {_scrapingbee_primary}")


def _should_skip(url: str) -> bool:
    """True if the URL is on the hopeless list — login-walled social media or
    metered paywalls where fetching wastes time and credits. Hostname match
    uses suffix semantics (so `x.com` also skips subdomains) and is strict
    about domain boundaries so `t.co` doesn't accidentally match
    `reddi<t.co>m`."""
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if host:
        parts = host.split(".")
        for i in range(len(parts) - 1):
            if ".".join(parts[i:]) in SKIP_HOSTS:
                return True
    u = url.lower()
    return any(p in u for p in SKIP_PATH_PREFIXES)


def _is_usable(text: str | None) -> bool:
    """True if the extracted text looks like real article content — not a
    bot-challenge page, login wall, or nav shell."""
    if not text or len(text.strip()) < _MIN_GOOD_BODY:
        return False
    return not is_bot_wall(text) and not is_chrome(text)


def _fetch_raw_html(url: str, timeout: int = 15) -> Optional[str]:
    """Pull HTML via urllib with a browser UA. Returns None on error, non-HTML
    Content-Type, or response too small to be useful."""
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "xml" not in ctype:
                return None
            body = resp.read(_HTTP_READ_CAP)
            return body.decode("utf-8", errors="ignore")
    except Exception:
        return None


def _extract_page_date(html: str) -> Optional[str]:
    """Pull the article's own published date from HTML metadata (OG tags,
    schema.org, article:published_time, JSON-LD). Returns a 'YYYY-MM-DD'
    string or None. Trafilatura walks every sensible source; we trust its
    extraction and fall back to None if it can't find one."""
    if not _HAS_TRAFILATURA or not html:
        return None
    try:
        md = trafilatura.extract_metadata(html)
        if md and getattr(md, "date", None):
            return str(md.date)
    except Exception:
        pass
    return None


def _stash_page_date(url: Optional[str], html: Optional[str]) -> None:
    """Best-effort: remember the page date so `get_page_date(url)` can
    return it later. Never raises — date extraction is enrichment, not
    required for the fetch to succeed."""
    if not url or not html:
        return
    d = _extract_page_date(html)
    if d:
        with _cache_lock:
            _page_dates[url] = d


def _try_trafilatura(html: str, url: Optional[str] = None) -> Optional[str]:
    if not _HAS_TRAFILATURA or not html:
        return None
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            favor_recall=True,   # prefer more text over stricter precision
            no_fallback=True,
        )
        if _is_usable(text):
            _stash_page_date(url, html)
            return text.strip()
    except Exception:
        pass
    return None


def get_page_date(url: str) -> Optional[str]:
    """Return the page's own publication date ('YYYY-MM-DD') if we
    extracted one during fetch, else None. Safe to call before any fetch;
    just returns None."""
    if not url:
        return None
    with _cache_lock:
        return _page_dates.get(url)


# Reddit JSON fast-path. Reddit's React SPA extracts badly (ZenRows/trafilatura
# typically return <500 chars of chrome) but every /comments/ URL has a clean
# public JSON mirror at {url}.json — no auth needed. We flatten post body +
# comment tree into text. This avoids burning paid-scraper credits on Reddit
# URLs that wouldn't extract well anyway.
_REDDIT_COMMENTS_RE = re.compile(r"reddit\.com/r/[^/]+/comments/", re.IGNORECASE)


def _try_reddit_json(url: str, timeout: int = 15) -> Optional[str]:
    if not _REDDIT_COMMENTS_RE.search(url or ""):
        return None

    clean = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    json_url = clean + ".json"
    headers = {
        # Reddit rate-limits generic browser UAs hard but accepts descriptive
        # bot UAs per their API etiquette docs.
        "User-Agent": "competitor-watch/1.0 (research bot)",
        "Accept": "application/json",
    }

    try:
        req = urllib.request.Request(json_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(_HTTP_READ_CAP).decode("utf-8", errors="ignore"))
    except Exception as e:
        print(f"[reddit-json] error on {url}: {e}")
        return None

    if not isinstance(data, list) or len(data) < 2:
        return None

    parts: list[str] = []
    try:
        post = data[0]["data"]["children"][0]["data"]
        title = (post.get("title") or "").strip()
        selftext = (post.get("selftext") or "").strip()
        author = post.get("author") or ""
        if title:
            parts.append(f"# {title}")
        if selftext:
            parts.append(f"[{author}]: {selftext}" if author else selftext)
        # Reddit's own timestamp for the post is more reliable than any
        # search provider's "published" string. Stash it as YYYY-MM-DD.
        created_utc = post.get("created_utc")
        if created_utc:
            import datetime as _dt
            try:
                d = _dt.datetime.utcfromtimestamp(float(created_utc)).strftime("%Y-%m-%d")
                with _cache_lock:
                    _page_dates[url] = d
            except (TypeError, ValueError, OSError):
                pass
    except (KeyError, IndexError, TypeError):
        pass

    def _walk(children):
        for c in children or []:
            if not isinstance(c, dict) or c.get("kind") != "t1":
                continue
            d = c.get("data") or {}
            body = (d.get("body") or "").strip()
            author = d.get("author") or ""
            if body and body not in ("[deleted]", "[removed]"):
                parts.append(f"[{author}]: {body}" if author else body)
            replies = d.get("replies")
            if isinstance(replies, dict):
                _walk((replies.get("data") or {}).get("children"))

    try:
        _walk(data[1]["data"]["children"])
    except (KeyError, TypeError):
        pass

    text = "\n\n".join(parts).strip()
    if len(text) < _MIN_GOOD_BODY:
        return None
    return text


# ScrapingBee — paid tier that uses residential/premium proxies + a real
# headless browser. Clears Cloudflare Turnstile, Akamai, and most bot-walls.
# Used as ZenRows fallback (or primary if the scrapingbee_primary toggle is on).
#
# Pricing (your tier):
#   - JS rendering:   5 credits
#   - Premium proxy: 25 credits per request (required for CF job boards)
# $49/mo plan = 150,000 credits ⇒ ~6,000 premium requests/mo (ample headroom
# for ~10-20 CF URLs per scan × 1 scan/day).
#
# ScrapingBee doesn't charge for failed requests (4xx/5xx), so we only
# record usage on success.
_SCRAPINGBEE_USD_PER_CREDIT = 49.0 / 150_000  # ≈ $0.000327


def _try_scrapingbee(url: str, timeout: int = 60) -> Optional[str]:
    key = os.environ.get("SCRAPINGBEE_API_KEY", "")
    if not key:
        return None
    params = {
        "api_key":        key,
        "url":            url,
        "render_js":      "true",
        "premium_proxy":  "true",    # needed for Cloudflare sites
        "country_code":   "us",      # avoid EU GDPR consent walls (Yahoo, etc.)
        "block_ads":      "true",    # speeds up render
        "block_resources": "false",  # we NEED the article HTML
    }
    sb_url = "https://app.scrapingbee.com/api/v1/?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(sb_url, headers={"User-Agent": "competitor-watch/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(_HTTP_READ_CAP).decode("utf-8", errors="ignore")
    except Exception as e:
        # Failures aren't billed, but log them so you can see the miss rate.
        print(f"[scrapingbee] error on {url}: {e}")
        _record_scrapingbee_usage(success=False, credits=0)
        return None

    # Record the successful request — ScrapingBee returned SOME HTML.
    # Premium proxy is 25 credits per request regardless of what we do with it.
    _record_scrapingbee_usage(success=True, credits=25)

    # Now extract with trafilatura — ScrapingBee returns raw HTML.
    text = _try_trafilatura(html, url=url)
    if text:
        return text
    # Fallback: if trafilatura fails but we got HTML, try a looser extract
    # (no recall/precision tweaks) — some sites have unusual structure.
    if _HAS_TRAFILATURA:
        try:
            loose = trafilatura.extract(html, include_comments=False,
                                        include_tables=False)
            if _is_usable(loose):
                _stash_page_date(url, html)
                return loose.strip()
        except Exception:
            pass
    return None


# ZenRows — paid scraping API similar to ScrapingBee but with its own proxy
# pool and JS-rendering pipeline. We default to ZenRows as the primary fetcher
# when its key is set; ScrapingBee then becomes the secondary paid fallback.
#
# Pricing override: set ZENROWS_USD_PER_CREDIT in env to track real spend.
# Default cost-per-call below is a placeholder — tune to your plan.
_ZENROWS_USD_PER_CREDIT = 0.0010
_ZENROWS_CREDITS_PER_CALL = 25  # premium proxy + JS render = ~25 credits


def _try_zenrows(url: str, timeout: int = 60) -> Optional[str]:
    key = os.environ.get("ZENROWS_API_KEY", "")
    if not key:
        return None
    params = {
        "apikey":          key,
        "url":             url,
        "js_render":       "true",
        "premium_proxy":   "true",   # needed for Cloudflare / heavily-protected sites
        "proxy_country":   "us",     # avoid EU consent walls
        "block_resources": "image,media,font",
    }
    z_url = "https://api.zenrows.com/v1/?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(z_url, headers={"User-Agent": "competitor-watch/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(_HTTP_READ_CAP).decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[zenrows] error on {url}: {e}")
        _record_zenrows_usage(success=False, credits=0)
        return None

    _record_zenrows_usage(success=True, credits=_ZENROWS_CREDITS_PER_CALL)

    text = _try_trafilatura(html, url=url)
    if text:
        return text
    if _HAS_TRAFILATURA:
        try:
            loose = trafilatura.extract(html, include_comments=False,
                                        include_tables=False)
            if _is_usable(loose):
                _stash_page_date(url, html)
                return loose.strip()
        except Exception:
            pass
    return None


def _record_zenrows_usage(*, success: bool, credits: int):
    """Log the call to usage_events so /settings/usage shows the spend."""
    try:
        from .db import SessionLocal
        from .models import UsageEvent
        from .usage import current_run_id
        rate = float(os.environ.get(
            "ZENROWS_USD_PER_CREDIT", str(_ZENROWS_USD_PER_CREDIT)
        ))
        db = SessionLocal()
        try:
            db.add(UsageEvent(
                run_id=current_run_id.get(),
                provider="zenrows",
                operation="fetch",
                model="premium_proxy",
                credits=credits,
                cost_usd=credits * rate,
                success=success,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def _record_scrapingbee_usage(*, success: bool, credits: int):
    """Log the call to usage_events so /settings/usage shows the spend."""
    try:
        from .db import SessionLocal
        from .models import UsageEvent
        from .usage import current_run_id
        rate = float(os.environ.get(
            "SCRAPINGBEE_USD_PER_CREDIT", str(_SCRAPINGBEE_USD_PER_CREDIT)
        ))
        db = SessionLocal()
        try:
            db.add(UsageEvent(
                run_id=current_run_id.get(),
                provider="scrapingbee",
                operation="fetch",
                model="premium_proxy",
                credits=credits,
                cost_usd=credits * rate,
                success=success,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def _finalize(text: str) -> str:
    """Strip nav/image boilerplate; keep full article body. The only cap is
    RAW_CONTENT_MAX as a safety bound against pathologically large pages —
    normal articles pass through untouched."""
    cleaned = clean_extracted(text)
    if len(cleaned) > RAW_CONTENT_MAX:
        cleaned = cleaned[:RAW_CONTENT_MAX]
    return cleaned


def fetch_article(url: str) -> tuple[Optional[str], str]:
    """Fetch + extract one URL.
    Returns (content, source) where source ∈ {'reddit-json', 'zenrows',
    'scrapingbee', 'trafilatura', 'skipped', 'failed', 'cached'}. Content
    is None for skipped/failed."""
    if not url:
        return (None, "failed")

    with _cache_lock:
        if url in _cache:
            content, src = _cache[url]
            return (content, "cached" if src not in ("skipped", "failed") else src)

    if _should_skip(url):
        result = (None, "skipped")
        with _cache_lock:
            _cache[url] = result
        return result

    has_zenrows = bool(os.environ.get("ZENROWS_API_KEY", ""))
    has_scrapingbee = bool(os.environ.get("SCRAPINGBEE_API_KEY", ""))

    def _cache_and_return(content: Optional[str], source: str) -> tuple[Optional[str], str]:
        res = (_finalize(content) if content else None, source)
        with _cache_lock:
            _cache[url] = res
        return res

    # Reddit fast-path: /comments/ URLs fetch much better via the public JSON
    # mirror than via any HTML scraper. Try this before burning paid credits —
    # fall through to the normal cascade only if Reddit refuses us (rate limit
    # or geo-block), which is rare.
    if _REDDIT_COMMENTS_RE.search(url):
        text = _try_reddit_json(url)
        if text:
            return _cache_and_return(text, "reddit-json")

    # ZenRows-primary path (default when key is set): ZenRows first, then
    # ScrapingBee, then free tier. Toggled via /settings/providers.
    if has_zenrows and _zenrows_primary:
        text = _try_zenrows(url)
        if text:
            return _cache_and_return(text, "zenrows")
        if has_scrapingbee:
            text = _try_scrapingbee(url)
            if text:
                return _cache_and_return(text, "scrapingbee")
        html = _fetch_raw_html(url)
        text = _try_trafilatura(html, url=url) if html else None
        if text:
            return _cache_and_return(text, "trafilatura")
        return _cache_and_return(None, "failed")

    # Paid-primary path: ScrapingBee first, free tier only as outage fallback.
    # Switched on via /settings/providers (the "Use ScrapingBee as primary
    # fetcher" toggle writes config.fetcher.scrapingbee_primary = true).
    if has_scrapingbee and _scrapingbee_primary:
        text = _try_scrapingbee(url)
        if text:
            return _cache_and_return(text, "scrapingbee")
        html = _fetch_raw_html(url)
        text = _try_trafilatura(html, url=url) if html else None
        if text:
            return _cache_and_return(text, "trafilatura")
        return _cache_and_return(None, "failed")

    # Free-primary path: trafilatura → ZenRows → ScrapingBee (only if keys set).
    # Keeps paid spend low — only triggers when free extraction fails.
    html = _fetch_raw_html(url)
    text = _try_trafilatura(html, url=url) if html else None
    if text:
        return _cache_and_return(text, "trafilatura")
    if has_zenrows:
        text = _try_zenrows(url)
        if text:
            return _cache_and_return(text, "zenrows")
    if has_scrapingbee:
        text = _try_scrapingbee(url)
        if text:
            return _cache_and_return(text, "scrapingbee")
    return _cache_and_return(None, "failed")


def bulk_fetch(urls: list[str], concurrency: int = 4) -> dict[str, tuple[Optional[str], str]]:
    """Fetch many URLs in parallel. Fixed small concurrency — we're being
    polite to target sites. Returns {url: (content, source)}.

    Prints a summary line to the terminal so you can see how the fetcher
    performed on each scan: `[fetch] 12 URLs -> zenrows=8 trafilatura=3 skipped=1`."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    urls = [u for u in (urls or []) if u]
    if not urls:
        return {}

    out: dict[str, tuple[Optional[str], str]] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fetch_article, u): u for u in urls}
        for fut in as_completed(futures):
            u = futures[fut]
            try:
                out[u] = fut.result()
            except Exception:
                out[u] = (None, "failed")

    tally: dict[str, int] = {}
    total_chars = 0
    for content, src in out.values():
        tally[src] = tally.get(src, 0) + 1
        if content:
            total_chars += len(content)
    summary = " ".join(f"{k}={v}" for k, v in sorted(tally.items()))
    avg = (total_chars // max(1, sum(1 for c, _ in out.values() if c))) if total_chars else 0
    print(f"[fetch] {len(out)} URLs -> {summary} | avg {avg} chars")
    return out
