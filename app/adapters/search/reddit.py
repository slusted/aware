"""Reddit collector — unified subreddit sweep producing 'voice of customer'
findings.

Before this module lived here, the same logic existed twice: once inside
`scanner.scan_competitor` (keyword-union sweep per sub) and once in
`app/customer_watch.scan_customer_sources` (name-scoped sweep per sub ×
competitor). This module owns the shared shape — query construction,
filtering, finding normalization — so each caller only picks its
iteration mode.

Both collectors route every query through `scanner.search_tavily` via
module-attribute access so the `install_scanner_hook` fan-out + enrichment
pipeline kicks in. A `from scanner import search_tavily` would bind the
pre-hook reference at import time.
"""
from __future__ import annotations

import scanner as _scanner
from scanner import is_new, mark_seen, content_hash
from app.adapters.fetch.sanitize import is_chrome, is_noise


def _subreddit_query(subreddit: str, text: str) -> str:
    return f"{text} site:reddit.com/r/{subreddit}"


def _finding(r: dict, *, competitor: str, subreddit: str) -> dict:
    content = r.get("content") or ""
    return {
        "competitor": competitor,
        "source": f"reddit/r/{subreddit}",
        "topic": "voice of customer",
        "content": content,
        "snippet": r.get("snippet", ""),
        "title": r.get("title", ""),
        "url": r.get("url", ""),
        "relevance": r.get("score", 0),
        "search_provider": r.get("source_provider", "tavily"),
        "published": r.get("published", ""),
        "hash": content_hash(content + (r.get("url") or "")),
    }


def _filter_and_normalize(
    results: list[dict],
    *,
    memory: dict,
    competitor: str,
    subreddit: str,
    min_score: float = 0.0,
    min_len: int = 60,
) -> list[dict]:
    out: list[dict] = []
    for r in results:
        content = r.get("content") or ""
        if not content or len(content) < min_len:
            continue
        # is_noise is safe to apply to Reddit — its patterns are job-board
        # boilerplate that Reddit threads essentially never contain.
        if is_chrome(content) or is_noise(content):
            continue
        if (r.get("score", 0) or 0) < min_score:
            continue
        if not is_new(content, memory):
            continue
        out.append(_finding(r, competitor=competitor, subreddit=subreddit))
        mark_seen(content, memory)
    return out


def collect_by_keyword_union(
    *,
    subreddits: list[str],
    keyword_any: str,
    competitor_name: str,
    memory: dict,
    max_results: int = 5,
    min_score: float = 0.0,
    min_len: int = 50,
) -> list[dict]:
    """Scanner's mode — one query per sub with keywords OR-joined into a
    single clause. Callers pass `social_min` as `min_score` to enforce the
    higher bar that VoC noise warrants."""
    findings: list[dict] = []
    for sub in subreddits:
        results = _scanner.search_tavily(
            _subreddit_query(sub, keyword_any),
            search_depth="advanced",
            max_results=max_results,
            include_raw=True,
        )
        findings.extend(_filter_and_normalize(
            results,
            memory=memory,
            competitor=competitor_name,
            subreddit=sub,
            min_score=min_score,
            min_len=min_len,
        ))
    return findings


def collect_by_competitor_name(
    *,
    subreddits: list[str],
    competitors: list[dict],
    memory: dict,
    max_results: int = 6,
    min_score: float = 0.0,
    min_len: int = 60,
) -> list[dict]:
    """customer_watch's mode — one query per (sub × competitor). The
    name-scoped query is tight enough that a score threshold is usually
    unnecessary (default 0)."""
    findings: list[dict] = []
    for sub in subreddits:
        sub_new = 0
        for c in competitors:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            quoted = f'"{name}"' if " " in name else name
            print(f"[customer] scanning r/{sub} for {name}...")
            try:
                results = _scanner.search_tavily(
                    _subreddit_query(sub, quoted),
                    search_depth="advanced",
                    max_results=max_results,
                    include_raw=True,
                )
            except Exception as e:
                print(f"[customer] error on r/{sub} × {name}: {e}")
                continue
            new = _filter_and_normalize(
                results,
                memory=memory,
                competitor=name,
                subreddit=sub,
                min_score=min_score,
                min_len=min_len,
            )
            findings.extend(new)
            sub_new += len(new)
        print(f"[customer] r/{sub}: {sub_new} new across {len(competitors)} competitors")
    return findings
