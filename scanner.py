"""
Scanner — searches web, news, and Reddit for competitor mentions.
Uses Tavily API (purpose-built for AI agents) with DuckDuckGo fallback.
"""

import urllib.request
import urllib.parse
import hashlib
import json
import os
from datetime import datetime

from app.adapters.fetch.sanitize import (
    EXCLUDE_DOMAINS,
    RAW_CONTENT_MAX,
    clean_extracted,
    is_bot_wall,
    is_chrome,
    is_noise,
)
# Tavily lives in its own adapter module. The `search_tavily` re-export below
# preserves the legacy `scanner.search_tavily` name that
# `app/search_providers/__init__.py:install_scanner_hook` monkey-patches at
# startup to add fan-out + enrichment. `_tavily_adapter` is kept as a module
# handle so reads like `_tavily_adapter.TAVILY_API_KEY` see live state after
# `app/env_keys.py` rotates the key.
from app.search_providers import tavily as _tavily_adapter
from app.search_providers.tavily import search_tavily


DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
MEMORY_FILE = os.path.join(DATA_DIR, "seen_items.json")


def load_memory() -> dict:
    """Load persisted memory + attach a fast-lookup set for `seen_hashes`.

    `seen_hashes` remains a list on disk (ordered, capped by recency), but in
    memory we keep a parallel set under `_seen_set` so the hot-path `is_new`
    check is O(1) instead of scanning a 2000-entry list per finding. The
    underscore prefix marks it as non-persistable — `save_memory` strips it.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    defaults = {
        "seen_hashes": [],
        "last_run": None,
        "run_count": 0,
        "insights": [],
        "report_summaries": [],
        "competitor_profiles": {},
        "trends": [],
    }
    memory = defaults
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                loaded = json.load(f)
            memory = {**defaults, **loaded}
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [memory] WARNING: could not load {MEMORY_FILE} ({e}); using defaults")
    # Derived fast-lookup set (non-persistable — stripped at save time).
    memory["_seen_set"] = set(memory.get("seen_hashes", []))
    return memory


def save_memory(memory: dict):
    memory["seen_hashes"] = memory["seen_hashes"][-2000:]
    memory["insights"] = memory["insights"][-100:]
    memory["report_summaries"] = memory["report_summaries"][-30:]
    memory["trends"] = memory["trends"][-50:]
    os.makedirs(DATA_DIR, exist_ok=True)
    # Strip underscore-prefixed keys — those are derived/in-memory only
    # (e.g. _seen_set). JSON can't serialize sets anyway, so this is required
    # for correctness, not just cleanliness.
    persistable = {k: v for k, v in memory.items() if not k.startswith("_")}
    with open(MEMORY_FILE, "w") as f:
        json.dump(persistable, f, indent=2)


def content_hash(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode()).hexdigest()[:12]


import threading
_SEEN_LOCK = threading.Lock()


def is_new(text: str, memory: dict) -> bool:
    """Fast membership check. Reads the set unlocked — CPython's GIL makes
    individual `x in set` / `set.add(y)` operations atomic, so reads are
    safe even while another thread is adding. Falls back to the list if
    `_seen_set` wasn't attached (e.g. legacy callers that built `memory`
    by hand)."""
    h = content_hash(text)
    s = memory.get("_seen_set")
    if s is not None:
        return h not in s
    return h not in memory.get("seen_hashes", [])


def mark_seen(text: str, memory: dict):
    """Record a hash as seen. Updates both the set (for fast lookup) and the
    list (for ordering/persistence) under a single lock so they stay in sync."""
    h = content_hash(text)
    with _SEEN_LOCK:
        s = memory.setdefault("_seen_set", set(memory.get("seen_hashes", [])))
        if h in s:
            return
        s.add(h)
        memory.setdefault("seen_hashes", []).append(h)


# ═══════════════════════════════════════════════════════════════
#  SEARCH — Tavily (primary) with DuckDuckGo fallback
# ═══════════════════════════════════════════════════════════════

def search_ddg_fallback(query: str) -> list[dict]:
    """Fallback: DuckDuckGo HTML scraping. Returns same format as Tavily."""
    urls = [
        "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query),
        "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query),
    ]
    for url in urls:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            results = []
            parts = html.split('class="result__snippet"')
            for part in parts[1:8]:
                snippet = part.split(">", 1)[-1].split("</")[0]
                snippet = snippet.replace("<b>", "").replace("</b>", "").strip()
                if snippet and len(snippet) > 20:
                    results.append({"title": "", "content": snippet, "url": "", "score": 0})
            if results:
                return results
        except Exception:
            continue
    return []


def search_web(query: str, topic: str = "general") -> list[str]:
    """Search the web. Uses Tavily if available, falls back to DuckDuckGo.
    Returns list of content strings for backward compatibility."""
    if _tavily_adapter.TAVILY_API_KEY:
        results = search_tavily(query, topic=topic)
    else:
        results = search_ddg_fallback(query)
    return [r["content"] for r in results if r.get("content")]


def search_web_rich(query: str, topic: str = "general", depth: str = "basic") -> list[dict]:
    """Search the web, returning rich results with title, content, url, score."""
    if _tavily_adapter.TAVILY_API_KEY:
        return search_tavily(query, search_depth=depth, topic=topic)
    return search_ddg_fallback(query)


def search_reddit(query: str, subreddit: str = None) -> list[str]:
    """Search Reddit content."""
    if _tavily_adapter.TAVILY_API_KEY:
        site_filter = f"site:reddit.com/r/{subreddit}" if subreddit else "site:reddit.com"
        return search_web(f"{query} {site_filter}")
    else:
        site = f"site:reddit.com/r/{subreddit}" if subreddit else "site:reddit.com"
        return search_web(f"{query} {site}")


def search_news(query: str) -> list[str]:
    """Search recent news."""
    if _tavily_adapter.TAVILY_API_KEY:
        # Tavily has a dedicated "news" topic that returns recent results
        return search_web(query, topic="news")
    return search_web(f"{query} news {datetime.now().year}")


# ═══════════════════════════════════════════════════════════════
#  MAIN SCAN
# ═══════════════════════════════════════════════════════════════

# Scan limits — overridden per-run by configure_limits(config["limits"]). The
# env fallback for min_relevance keeps pre-config deployments working until
# they're updated. Tavily's score is 0.0–1.0; results below ~0.25 are almost
# always off-topic. Serper hardcodes 0.5, so thresholds above that start
# dropping Serper results entirely (see app/search_providers/__init__.py
# enrichment gate).
_LIMITS: dict = {
    "max_findings_per_competitor": 25,
    "max_findings_total": 60,
    "min_relevance": float(os.environ.get("MIN_RELEVANCE_SCORE", "0.25")),
}


def configure_limits(limits: dict | None) -> None:
    """Update module-level scan limits from a config block. Unknown or missing
    keys keep their current value. Call at the top of every scan entry point
    so per-competitor scan_competitor() sees the right min_relevance."""
    if not limits:
        return
    for k in ("max_findings_per_competitor", "max_findings_total"):
        if k in limits and limits[k] is not None:
            _LIMITS[k] = int(limits[k])
    if "min_relevance" in limits and limits["min_relevance"] is not None:
        _LIMITS["min_relevance"] = float(limits["min_relevance"])


def current_min_relevance() -> float:
    """Exposed so other modules (e.g. search_providers) can gate work on the
    same threshold without re-importing a constant that may be stale."""
    return _LIMITS["min_relevance"]

# Garbage titles that indicate an extraction failure — skip these entirely.
_GARBAGE_TITLES = {"page_title", "title", "untitled", ""}


def _is_garbage_title(title: str) -> bool:
    if not title or title.strip().lower() in _GARBAGE_TITLES:
        return True
    return False

# Patterns that indicate a result is a job listing, not competitive intelligence
def scan_competitor(competitor: dict, topics: list[str], memory: dict) -> list[dict]:
    """Scan one competitor and return new findings, filtered and capped.

    Strategy:
    1. FIRST: hit their official newsrooms/product blogs (highest signal)
    2. THEN: search third-party press for coverage (different perspective)
    3. THEN: strategic hiring, voice of customer, etc.
    """
    name = competitor["name"]
    findings = []

    # Per-competitor score overrides — fall back to the global env-driven
    # defaults when the competitor doesn't specify its own threshold. Tunable
    # in /admin/competitors/<id>/edit.
    min_score = competitor.get("min_relevance_score")
    if min_score is None:
        min_score = _LIMITS["min_relevance"]
    else:
        min_score = float(min_score)
    social_mult = competitor.get("social_score_multiplier")
    if social_mult is None:
        social_mult = 1.5
    else:
        social_mult = float(social_mult)
    social_min = min_score * social_mult

    # Seeds for every keyword-driven search phase. If keywords[] is empty we
    # fall back to the competitor name so untuned competitors still get scanned.
    keyword_seeds = [k.strip() for k in (competitor.get("keywords") or []) if k and k.strip()]
    if not keyword_seeds:
        keyword_seeds = [name]

    def _q(seed: str) -> str:
        return f'"{seed}"' if " " in seed else seed

    # OR-joined keyword clause, used when we want "any keyword matches"
    # across a single query (e.g. within one subreddit).
    keyword_any = "(" + " OR ".join(_q(k) for k in keyword_seeds) + ")"

    # ── PRIORITY: Official newsroom & product blog ────────────────
    # These are the horse's mouth — product announcements, feature releases,
    # company news directly from the competitor. Highest signal source.
    # Fanned per keyword so sub-products (e.g. "LinkedIn Recruiter") each
    # drive their own domain-scoped sweep.
    newsroom_domains = competitor.get("newsroom_domains", [])
    press_domains = competitor.get("press_domains", [])
    all_official_domains = newsroom_domains + press_domains

    if all_official_domains:
        # Keep attribution: each result carries the seed that produced it so
        # we can log matched_keyword on the resulting finding. Flattening
        # into one list across seeds would erase that.
        newsroom_results: list[tuple[str, dict]] = []
        for seed in keyword_seeds:
            qs = _q(seed)
            # Product announcements
            for r in search_tavily(
                f'{qs} (launch OR feature OR update OR announcement OR release OR roadmap)',
                search_depth="advanced",
                max_results=5,
                include_domains=all_official_domains,
                include_raw=True,
            ):
                newsroom_results.append((seed, r))
            # Engineering/tech blog posts
            for r in search_tavily(
                f'{qs} (engineering OR AI OR "machine learning" OR automation OR architecture)',
                search_depth="advanced",
                max_results=3,
                include_domains=all_official_domains,
                include_raw=True,
            ):
                newsroom_results.append((seed, r))

        for seed, r in newsroom_results:
            content = r["content"]
            if not content or len(content) < 50:
                continue
            if is_new(content, memory):
                findings.append({
                    "competitor": name,
                    "source": "official",
                    "topic": "product announcement",
                    "content": content,
                    "snippet": r.get("snippet", ""),
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "relevance": r.get("score", 0) + 0.2,  # boost official sources
                    "search_provider": r.get("source_provider", "tavily"),
                    "published": r.get("published", ""),
                    "matched_keyword": seed,
                })
                mark_seen(content, memory)

    year = datetime.now().year

    # ── Keyword-driven fan-out ─────────────────────────────────────
    # Each keyword becomes the subject of its own product/strategy/AI/news
    # search. `keyword_seeds` + `_q` were hoisted to the top of this function
    # so the newsroom, careers, and reddit phases can share them.
    for seed in keyword_seeds:
        qs = _q(seed)
        search_plan = [
            # product/feature: launches, rollouts, new capabilities
            (f'{qs} (product launch OR "new feature" OR "product update" OR rollout) -review -tips -guide -"how to" {year}',
             "product/feature", "general"),
            # strategy/business: pricing, M&A, partnerships, funding
            (f'{qs} (pricing OR acquisition OR partnership OR funding OR IPO) {year}',
             "strategy/business", "general"),
            # AI/technology: AI features, ML, automation
            (f'{qs} AI (hiring OR recruitment OR matching OR automation) -"how to" -tips {year}',
             "AI/technology", "general"),
            # news topic: recent headlines
            (qs, "news", "news"),
        ]

        for query, topic_label, tavily_topic in search_plan:
            results = search_tavily(
                query,
                search_depth="advanced",
                topic=tavily_topic,
                max_results=5,
                exclude_domains=EXCLUDE_DOMAINS,
                include_raw=True,
            )
            for r in results:
                content = r["content"]
                title = r.get("title", "")
                if not content or len(content) < 50:
                    continue
                if is_noise(content) or is_chrome(content):
                    continue
                if _is_garbage_title(title) and len(content) < 200:
                    continue
                if r.get("score", 0) < min_score:
                    continue
                if is_new(content, memory):
                    findings.append({
                        "competitor": name,
                        "source": "web" if tavily_topic == "general" else "news",
                        "topic": topic_label,
                        "content": content,
                        "snippet": r.get("snippet", ""),
                        "title": title,
                        "url": r.get("url", ""),
                        "relevance": r.get("score", 0),
                        "search_provider": r.get("source_provider", "tavily"),
                        "published": r.get("published", ""),
                        "matched_keyword": seed,
                    })
                    mark_seen(content, memory)

    # ── Search type 5: Strategic hiring signals ─────────────────────
    # What are they hiring for? This reveals product roadmap.
    # Scoped explicitly to careers_domains so results are from THEIR sites —
    # previously the query was domain-free and pulled job-board noise.
    careers_domains = competitor.get("careers_domains", [])

    # Keep seed attribution on the keyword-driven sweep; ATS results use
    # the company name so they stay unattributed (matched_keyword=None).
    hiring_results: list[tuple[str | None, dict]] = []
    if careers_domains:
        for seed in keyword_seeds:
            for r in search_tavily(
                f'{_q(seed)} (hiring OR engineer OR "product manager" OR AI OR ML OR platform)',
                search_depth="advanced",
                max_results=4,
                include_domains=careers_domains,
                include_raw=True,
            ):
                hiring_results.append((seed, r))

    # Also search Greenhouse/Lever boards which many use for ATS.
    # Uses the company name (not keywords) — these boards list the parent org.
    ats_results = search_tavily(
        f"{name} site:greenhouse.io OR site:lever.co OR site:ashbyhq.com",
        search_depth="basic",
        max_results=3,
        include_raw=True,
    )
    for r in ats_results:
        hiring_results.append((None, r))

    for seed, r in hiring_results:
        content = r["content"]
        title = r.get("title", "")
        if not content or len(content) < 50:
            continue
        if _is_garbage_title(title) and len(content) < 200:
            continue
        if r.get("score", 0) < min_score:
            continue
        if is_new(content, memory):
            findings.append({
                "competitor": name,
                "source": "careers",
                "topic": "strategic hiring",
                "content": content,
                "snippet": r.get("snippet", ""),
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "relevance": r.get("score", 0),
                "search_provider": r.get("source_provider", "tavily"),
                "published": r.get("published", ""),
                "matched_keyword": seed,
            })
            mark_seen(content, memory)

    # ── Search type 6: Voice of Customer ─────────────────────────────
    # Reddit + LinkedIn posts. Twitter/X dropped — its content cannot be
    # extracted (login wall returns profile/chrome boilerplate, not tweets).
    # If you want X coverage, use a dedicated source with API access.

    # Reddit sweep — one query per sub with keywords OR-joined. Delegated
    # to the shared adapter so the same logic covers customer_watch's
    # cross-competitor scan. VoC findings need a higher bar to filter noise.
    from app.adapters.search.reddit import collect_by_keyword_union
    findings.extend(collect_by_keyword_union(
        subreddits=competitor.get("subreddits", []) or [],
        keyword_any=keyword_any,
        competitor_name=name,
        memory=memory,
        max_results=5,
        min_score=social_min,
        min_len=50,
    ))

    # LinkedIn posts — professional commentary. Use snippet-only since
    # LinkedIn also frequently serves login walls.
    linkedin_query = (
        f'"{name}" (recruiter OR employer OR job seeker) '
        f'(experience OR review OR switched OR compared) '
        f'site:linkedin.com/posts OR site:linkedin.com/pulse'
    )
    linkedin_results = search_tavily(
        linkedin_query,
        search_depth="advanced",
        max_results=5,
        exclude_domains=None,  # we want social media sites
        include_raw=False,     # snippet-only — Google's cached preview beats the page
    )
    for r in linkedin_results:
        content = r.get("content", "")
        if not content or len(content) < 50:
            continue
        if is_noise(content) or is_chrome(content):
            continue
        if r.get("score", 0) < social_min:
            continue
        if is_new(content, memory):
            findings.append({
                "competitor": name,
                "source": "linkedin",
                "topic": "voice of customer",
                "content": content,
                "snippet": r.get("snippet", ""),
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "relevance": r.get("score", 0),
                "search_provider": r.get("source_provider", "tavily"),
                "published": r.get("published", ""),
            })
            mark_seen(content, memory)

    # Sort by relevance and cap
    cap = _LIMITS["max_findings_per_competitor"]
    findings.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    if len(findings) > cap:
        print(f"  [scan] Capped {name}: {len(findings)} → {cap}")
        findings = findings[:cap]

    return findings


def run_full_scan(config: dict) -> tuple[list[dict], dict]:
    """Scan all competitors. Returns (findings, updated_memory).
    Competitors are scanned in parallel via a threadpool to cut total wall time
    (each competitor fires 8–10 Tavily calls and they spend most of their time
    blocked on HTTP). Concurrency defaults to 4 — override with SCAN_CONCURRENCY."""
    configure_limits(config.get("limits"))
    memory = load_memory()
    all_findings = []

    search_type = "Tavily" if _tavily_adapter.TAVILY_API_KEY else "DuckDuckGo (fallback)"
    print(f"  [scan] Using {search_type}")

    concurrency = int(os.environ.get("SCAN_CONCURRENCY", "4"))
    competitors = list(config["competitors"])
    print(f"  [scan] Scanning {len(competitors)} competitors · concurrency={concurrency}")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_to_name = {
            pool.submit(scan_competitor, comp, config["watch_topics"], memory): comp["name"]
            for comp in competitors
        }
        for fut in as_completed(future_to_name):
            name = future_to_name[fut]
            try:
                findings = fut.result()
                all_findings.extend(findings)
                print(f"  [scan] {len(findings)} new items for {name}")
            except Exception as e:
                print(f"  [scan] ERROR scanning {name}: {e}")

    # Dedup by content
    seen = set()
    unique = []
    for f in all_findings:
        short = f["content"][:100].lower()
        if short not in seen:
            seen.add(short)
            unique.append(f)

    # Sort by relevance and cap total — analyst doesn't need 400+ items
    total_cap = _LIMITS["max_findings_total"]
    unique.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    if len(unique) > total_cap:
        print(f"  [scan] Capped total: {len(unique)} → {total_cap} (top by relevance)")
        unique = unique[:total_cap]

    memory["last_run"] = datetime.now().isoformat()
    memory["run_count"] = memory.get("run_count", 0) + 1
    save_memory(memory)

    print(f"  [scan] Total: {len(unique)} unique new findings")
    return unique, memory
