"""Customer-side voice-of-customer scanner — Reddit only.

For each active competitor, runs one Tavily search per configured subreddit
scoped to that single competitor name. Returns reddit thread URLs; our
search hook then enriches them via reddit-json so the stored content is OP
+ the full comment tree.

Findings are saved as:
    topic = "voice of customer"
    source = "reddit/r/<sub>"
    competitor = <the specific competitor the search was scoped to>

This is the same shape `scanner.scan_competitor` uses for its embedded VoC
section, so customer findings and competitor findings live in the same
table and unify naturally on the competitor profile page. The customer
page queries this topic to render the aggregated cross-competitor view.

Twitter/X was removed as a source: login-walled scrapes return chrome, and
even the JSON-free fallback snippets rarely contain enough signal. Invest
the API credits in deeper Reddit coverage instead.

Both call patterns (this file and scanner's per-competitor sweep) now
delegate to `app.adapters.search.reddit`, so query shape, filtering, and
finding normalization are shared.
"""
from app.adapters.search.reddit import collect_by_competitor_name


def scan_customer_sources(config: dict, memory: dict) -> list[dict]:
    """Run a per-competitor Reddit sweep across the configured subreddits.
    Returns a list of finding dicts with topic='voice of customer'.
    Shares the scanner's seen_hashes dedup."""
    watch = config.get("customer_watch") or {}
    if not watch.get("enabled", True):
        print("[customer] disabled in config")
        return []

    subs = watch.get("subreddits", [])
    max_results = int(watch.get("max_results_per_source", 6))
    competitors = config.get("competitors", [])

    if not competitors:
        print("[customer] no active competitors — skipping scan")
        return []
    if not subs:
        print("[customer] no subreddits configured — skipping scan")
        return []

    findings = collect_by_competitor_name(
        subreddits=subs,
        competitors=competitors,
        memory=memory,
        max_results=max_results,
    )
    print(f"[customer] total new VoC findings: {len(findings)}")
    return findings
