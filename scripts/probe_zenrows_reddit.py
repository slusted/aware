"""One-shot probe: can ZenRows pull the full Reddit comment tree?

The fetcher's Reddit path currently hits reddit.com/{url}.json directly via
urllib. That works locally but on Railway's shared egress Reddit returns
429/403 and we fall through to HTML scraping, which only yields the post
title + body (Reddit's React SPA doesn't server-render the comment tree).

This script tries several strategies on the same URL and reports:
  - HTTP status (via ZenRows `original_status=true` where relevant)
  - response length
  - whether the body parses as the expected Reddit JSON envelope
  - comment count extracted
  - first ~200 chars of body for eyeballing

Run:
  python scripts/probe_zenrows_reddit.py
  python scripts/probe_zenrows_reddit.py https://www.reddit.com/r/foo/comments/...
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

DEFAULT_URLS = [
    # Use a couple of real URLs from the dev DB — varied subreddits + ages.
    "https://www.reddit.com/r/humanresources/comments/1sqb9m5/caught_a_sixfigure_compliance_bomb_before_it_went/",
    "https://www.reddit.com/r/jobs/comments/1sq629d/i_stopped_applying_through_linkedin_2_months_ago/",
]

ZENROWS_KEY = os.environ.get("ZENROWS_API_KEY", "")


def _json_url(url: str) -> str:
    clean = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return clean + ".json"


def _count_comments(data) -> int:
    """Count t1 (comment) nodes in a Reddit listing response, recursing
    into replies. Returns 0 for anything that doesn't look like the
    expected envelope."""
    if not isinstance(data, list) or len(data) < 2:
        return 0
    total = 0

    def _walk(children):
        nonlocal total
        for c in children or []:
            if not isinstance(c, dict) or c.get("kind") != "t1":
                continue
            d = c.get("data") or {}
            body = (d.get("body") or "").strip()
            if body and body not in ("[deleted]", "[removed]"):
                total += 1
            replies = d.get("replies")
            if isinstance(replies, dict):
                _walk((replies.get("data") or {}).get("children"))

    try:
        _walk(data[1]["data"]["children"])
    except (KeyError, TypeError):
        pass
    return total


def _summarize(label: str, *, status: int | None, body: str | None, err: str | None,
               elapsed: float):
    print(f"\n=== {label}  ({elapsed:.1f}s) ===")
    if err:
        print(f"  ERROR: {err}")
        return
    print(f"  status: {status}")
    print(f"  len:    {len(body or '')}")
    if not body:
        return
    # Try parsing as JSON
    try:
        data = json.loads(body)
        n_comments = _count_comments(data)
        print(f"  JSON:   yes, comments={n_comments}")
        if n_comments == 0 and isinstance(data, list) and data:
            try:
                title = data[0]["data"]["children"][0]["data"].get("title", "")
                print(f"  title:  {title[:80]}")
            except (KeyError, IndexError, TypeError):
                pass
    except json.JSONDecodeError:
        snippet = body[:300].replace("\n", " ")
        print(f"  JSON:   no")
        print(f"  snip:   {snippet}")


def probe_urllib(url: str):
    """Baseline: what does urllib get when hitting .json directly?"""
    json_url = _json_url(url)
    headers = {
        "User-Agent": "competitor-watch/1.0 (research bot)",
        "Accept": "application/json",
    }
    t0 = time.time()
    try:
        req = urllib.request.Request(json_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            _summarize("urllib -> .json direct", status=resp.status, body=body,
                       err=None, elapsed=time.time() - t0)
    except urllib.error.HTTPError as e:
        _summarize("urllib -> .json direct", status=e.code, body=None,
                   err=f"HTTP {e.code} {e.reason}", elapsed=time.time() - t0)
    except Exception as e:
        _summarize("urllib -> .json direct", status=None, body=None, err=str(e),
                   elapsed=time.time() - t0)


def _zenrows_call(label: str, target: str, extra: dict):
    if not ZENROWS_KEY:
        print(f"\n=== {label} === SKIPPED (no ZENROWS_API_KEY)")
        return
    params = {
        "apikey": ZENROWS_KEY,
        "url":    target,
        "original_status": "true",
        **extra,
    }
    z_url = "https://api.zenrows.com/v1/?" + urllib.parse.urlencode(params)
    t0 = time.time()
    try:
        req = urllib.request.Request(z_url, headers={"User-Agent": "probe/1.0"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            # ZenRows surfaces the target's status in the Zr-Final-Url/headers
            # when original_status=true; easier to just check our own.
            body = resp.read().decode("utf-8", errors="ignore")
            # Useful ZenRows-specific headers:
            final_url = resp.headers.get("Zr-Final-Url") or ""
            concurrency = resp.headers.get("Concurrency-Limit") or ""
            if final_url:
                print(f"  (Zr-Final-Url: {final_url})")
            if concurrency:
                print(f"  (concurrency-limit: {concurrency})")
            _summarize(label, status=resp.status, body=body, err=None,
                       elapsed=time.time() - t0)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            err_body = ""
        _summarize(label, status=e.code, body=None,
                   err=f"HTTP {e.code} {e.reason} | {err_body}",
                   elapsed=time.time() - t0)
    except Exception as e:
        _summarize(label, status=None, body=None, err=str(e),
                   elapsed=time.time() - t0)


def probe_zenrows_variants(url: str):
    json_url = _json_url(url)

    # 1. ZenRows on .json, datacenter proxy (cheapest — 1 credit). No JS.
    _zenrows_call(
        "zenrows -> .json  [datacenter, no JS]",
        json_url,
        {},
    )

    # 2. ZenRows on .json with premium_proxy (residential, ~10 credits).
    #    This is the main hypothesis: residential IPs bypass Reddit's block.
    _zenrows_call(
        "zenrows -> .json  [premium_proxy=us]",
        json_url,
        {"premium_proxy": "true", "proxy_country": "us"},
    )

    # 3. Same as #2 but add antibot — advanced bypass (Cloudflare/DataDome).
    #    Overkill for Reddit's JSON endpoint but worth measuring.
    _zenrows_call(
        "zenrows -> .json  [premium_proxy + antibot]",
        json_url,
        {"premium_proxy": "true", "proxy_country": "us", "antibot": "true"},
    )

    # 4. ZenRows on the HTML URL with js_render — current config. Baseline
    #    to confirm the theory that HTML scraping only yields the post, not
    #    the comment tree.
    _zenrows_call(
        "zenrows -> HTML   [js_render + premium_proxy]  (current)",
        url,
        {"js_render": "true", "premium_proxy": "true", "proxy_country": "us",
         "block_resources": "image,media,font"},
    )


def main():
    urls = sys.argv[1:] or DEFAULT_URLS
    print(f"ZENROWS_API_KEY: {'set' if ZENROWS_KEY else 'MISSING'}")
    for url in urls:
        print(f"\n{'#' * 78}\n# {url}\n{'#' * 78}")
        probe_urllib(url)
        probe_zenrows_variants(url)


if __name__ == "__main__":
    main()
