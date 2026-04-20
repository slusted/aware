"""
Search Provider Benchmark
=========================
Runs the same competitive intelligence queries through multiple search APIs
and uses Claude to score the results. Helps you decide which provider to pay for.

Providers tested:
  1. Tavily    — AI-native search, advanced depth + raw content
  2. Brave     — Independent index, fast, $5/mo free credits (~1000 queries)
  3. Exa       — Semantic/neural search, 1000 free requests/mo
  4. Serper    — Google results, 2500 free queries

Usage:
  Set API keys for the providers you want to test (skip any you don't have):
    $env:TAVILY_API_KEY = "..."
    $env:BRAVE_API_KEY = "..."
    $env:EXA_API_KEY = "..."
    $env:SERPER_API_KEY = "..."
    $env:ANTHROPIC_API_KEY = "..."  (required — used for scoring)

  python benchmark_search.py
  python benchmark_search.py --query "custom search query here"
  python benchmark_search.py --full   (run all test queries — uses more credits)
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime

import anthropic

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")
BRAVE_KEY = os.environ.get("BRAVE_API_KEY", "")
EXA_KEY = os.environ.get("EXA_API_KEY", "")
SERPER_KEY = os.environ.get("SERPER_API_KEY", "")

# Test queries designed to exercise competitive intelligence use cases
TEST_QUERIES_QUICK = [
    # Product announcement from a specific newsroom
    "LinkedIn product launch hiring assistant AI 2026",
    # Business strategy / M&A
    "Indeed acquisition partnership recruitment 2026",
    # Voice of customer / sentiment
    "Greenhouse ATS review recruiter complaints switching",
]

TEST_QUERIES_FULL = TEST_QUERIES_QUICK + [
    # ATS moving into job distribution
    "Workday talent marketplace job distribution employer",
    # Labour hire going direct
    "Randstad direct hire platform technology AI matching",
    # Startup competitor
    "AI recruitment startup funding job matching 2026",
    # Specific newsroom content
    "site:news.linkedin.com product feature announcement",
    # News topic
    "ZipRecruiter new feature launch 2026",
]


# ═══════════════════════════════════════════════════════════════
#  SEARCH PROVIDERS
# ═══════════════════════════════════════════════════════════════

def search_tavily(query: str) -> list[dict]:
    """Tavily API — advanced depth with raw content."""
    if not TAVILY_KEY:
        return []
    body = {
        "query": query,
        "search_depth": "advanced",
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": "markdown",
    }
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TAVILY_KEY}",
        },
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - start
        results = []
        for r in data.get("results", []):
            raw = r.get("raw_content", "")
            content = r.get("content", "")
            if raw and len(raw) > 2000:
                raw = raw[:2000] + "..."
            results.append({
                "title": r.get("title", ""),
                "content": raw if raw else content,
                "snippet": content,
                "url": r.get("url", ""),
                "score": r.get("score", 0),
            })
        return results, elapsed
    except Exception as e:
        return [], time.time() - start


def search_brave(query: str) -> list[dict]:
    """Brave Search API."""
    if not BRAVE_KEY:
        return [], 0
    url = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(query)}&count=5"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_KEY,
    })
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_data = resp.read()
            # Handle gzip
            import gzip
            try:
                raw_data = gzip.decompress(raw_data)
            except Exception:
                pass
            data = json.loads(raw_data.decode("utf-8"))
        elapsed = time.time() - start
        results = []
        for r in data.get("web", {}).get("results", []):
            results.append({
                "title": r.get("title", ""),
                "content": r.get("description", ""),
                "snippet": r.get("description", ""),
                "url": r.get("url", ""),
                "score": 0,
            })
        return results, elapsed
    except Exception as e:
        return [], time.time() - start


def search_exa(query: str) -> list[dict]:
    """Exa AI — semantic search with contents."""
    if not EXA_KEY:
        return [], 0
    body = {
        "query": query,
        "numResults": 5,
        "contents": {
            "text": {"maxCharacters": 2000},
            "highlights": True,
        },
    }
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        "https://api.exa.ai/search",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": EXA_KEY,
        },
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - start
        results = []
        for r in data.get("results", []):
            text = r.get("text", "")
            highlights = r.get("highlights", [])
            content = text if text else " ".join(highlights)
            if len(content) > 2000:
                content = content[:2000] + "..."
            results.append({
                "title": r.get("title", ""),
                "content": content,
                "snippet": " ".join(highlights) if highlights else content[:200],
                "url": r.get("url", ""),
                "score": r.get("score", 0),
            })
        return results, elapsed
    except Exception as e:
        return [], time.time() - start


def search_serper(query: str) -> list[dict]:
    """Serper.dev — Google search results."""
    if not SERPER_KEY:
        return [], 0
    body = {"q": query, "num": 5}
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-KEY": SERPER_KEY,
        },
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - start
        results = []
        for r in data.get("organic", []):
            results.append({
                "title": r.get("title", ""),
                "content": r.get("snippet", ""),
                "snippet": r.get("snippet", ""),
                "url": r.get("link", ""),
                "score": 0,
            })
        return results, elapsed
    except Exception as e:
        return [], time.time() - start


PROVIDERS = {
    "tavily": {"fn": search_tavily, "key": TAVILY_KEY, "free_tier": "1,000/mo", "cost": "$0.008/search (advanced)"},
    "brave": {"fn": search_brave, "key": BRAVE_KEY, "free_tier": "$5 credit/mo (~1,000)", "cost": "$5/1,000"},
    "exa": {"fn": search_exa, "key": EXA_KEY, "free_tier": "1,000/mo + $10 credit", "cost": "$7/1,000"},
    "serper": {"fn": search_serper, "key": SERPER_KEY, "free_tier": "2,500 one-time", "cost": "$1/1,000"},
}


# ═══════════════════════════════════════════════════════════════
#  SCORING — Claude judges result quality
# ═══════════════════════════════════════════════════════════════

def score_results(query: str, provider_results: dict) -> dict:
    """Use Claude to score each provider's results for a given query."""
    client = anthropic.Anthropic()

    # Build comparison text
    comparison = f"QUERY: {query}\n\n"
    for provider, (results, elapsed) in provider_results.items():
        comparison += f"=== {provider.upper()} ({len(results)} results, {elapsed:.1f}s) ===\n"
        if not results:
            comparison += "(no results or API key not set)\n\n"
            continue
        for i, r in enumerate(results[:5]):
            comparison += f"  [{i+1}] {r['title']}\n"
            comparison += f"      URL: {r['url']}\n"
            comparison += f"      Content ({len(r['content'])} chars): {r['content'][:300]}...\n\n"
        comparison += "\n"

    prompt = f"""You are evaluating search API results for a competitive intelligence agent that monitors recruitment industry competitors for Seek (Australian job board).

{comparison}

Score each provider on these criteria (1-10 each):

1. **Relevance** — Do the results actually relate to competitive intelligence about the queried company? Or are they job listings, unrelated content, or noise?
2. **Content richness** — How much usable intelligence is in the content? Full articles > snippets > titles only
3. **Freshness** — Are results recent/current? Stale content from 2022 is useless for CI
4. **Signal-to-noise** — What percentage of results are genuinely useful vs garbage?
5. **Source quality** — Are results from credible sources (tech press, official blogs, business news) vs SEO spam?

Return ONLY valid JSON:
{{
  "provider_scores": {{
    "provider_name": {{
      "relevance": N,
      "content_richness": N,
      "freshness": N,
      "signal_to_noise": N,
      "source_quality": N,
      "total": N,
      "verdict": "one sentence summary"
    }}
  }},
  "winner": "provider_name",
  "analysis": "2-3 sentence comparison of the providers for this query type"
}}

Only score providers that returned results. Skip providers with 0 results."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        resp_text = response.content[0].text.strip()
        if resp_text.startswith("```"):
            resp_text = resp_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(resp_text)
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
#  MAIN BENCHMARK
# ═══════════════════════════════════════════════════════════════

def run_benchmark(queries: list[str]):
    """Run the full benchmark across all providers and queries."""

    # Check which providers are available
    available = {k: v for k, v in PROVIDERS.items() if v["key"]}
    if not available:
        print("ERROR: No search API keys set. Set at least one of:")
        print("  TAVILY_API_KEY, BRAVE_API_KEY, EXA_API_KEY, SERPER_API_KEY")
        return

    print("=" * 65)
    print("  Search Provider Benchmark for Competitor Watch")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 65)
    print(f"\n  Providers: {', '.join(available.keys())}")
    print(f"  Queries:   {len(queries)}")
    print(f"  API calls: ~{len(queries) * len(available)} total")

    unavailable = {k for k in PROVIDERS if k not in available}
    if unavailable:
        print(f"  Skipping:  {', '.join(unavailable)} (no API key)")

    print()

    all_scores = {}  # provider -> {criterion: [scores]}
    query_results = []

    for i, query in enumerate(queries):
        print(f"  [{i+1}/{len(queries)}] {query[:60]}...")

        # Run all providers
        provider_results = {}
        for name, provider in available.items():
            results, elapsed = provider["fn"](query)
            provider_results[name] = (results, elapsed)
            count = len(results) if isinstance(results, list) else 0
            print(f"    {name:10s}: {count} results in {elapsed:.1f}s")

        # Score with Claude
        print(f"    Scoring with Claude...")
        scores = score_results(query, provider_results)

        if "error" in scores:
            print(f"    Scoring failed: {scores['error']}")
            continue

        winner = scores.get("winner", "?")
        analysis = scores.get("analysis", "")
        print(f"    Winner: {winner}")
        if analysis:
            print(f"    {analysis[:100]}...")

        # Accumulate scores
        for provider, pscores in scores.get("provider_scores", {}).items():
            if provider not in all_scores:
                all_scores[provider] = {"totals": [], "relevance": [], "content_richness": [],
                                         "freshness": [], "signal_to_noise": [], "source_quality": []}
            for criterion in ["relevance", "content_richness", "freshness", "signal_to_noise", "source_quality"]:
                if criterion in pscores:
                    all_scores[provider][criterion].append(pscores[criterion])
            if "total" in pscores:
                all_scores[provider]["totals"].append(pscores["total"])

        query_results.append({
            "query": query,
            "scores": scores,
            "winner": winner,
        })
        print()

    # ── Summary ────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  OVERALL RESULTS")
    print("=" * 65)

    summary = {}
    for provider, scores in all_scores.items():
        avg_total = sum(scores["totals"]) / len(scores["totals"]) if scores["totals"] else 0
        summary[provider] = {
            "avg_total": round(avg_total, 1),
            "avg_relevance": round(sum(scores["relevance"]) / len(scores["relevance"]), 1) if scores["relevance"] else 0,
            "avg_richness": round(sum(scores["content_richness"]) / len(scores["content_richness"]), 1) if scores["content_richness"] else 0,
            "avg_freshness": round(sum(scores["freshness"]) / len(scores["freshness"]), 1) if scores["freshness"] else 0,
            "avg_noise": round(sum(scores["signal_to_noise"]) / len(scores["signal_to_noise"]), 1) if scores["signal_to_noise"] else 0,
            "avg_sources": round(sum(scores["source_quality"]) / len(scores["source_quality"]), 1) if scores["source_quality"] else 0,
            "free_tier": PROVIDERS[provider]["free_tier"],
            "cost": PROVIDERS[provider]["cost"],
        }

    # Sort by avg total
    ranked = sorted(summary.items(), key=lambda x: x[1]["avg_total"], reverse=True)

    print(f"\n  {'Provider':<12} {'Total':>6} {'Relev':>6} {'Rich':>6} {'Fresh':>6} {'S/N':>6} {'Source':>6}  Free Tier")
    print(f"  {'─'*12} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*6}  {'─'*20}")
    for provider, s in ranked:
        marker = " <-- BEST" if provider == ranked[0][0] else ""
        print(f"  {provider:<12} {s['avg_total']:>5.1f} {s['avg_relevance']:>6.1f} {s['avg_richness']:>6.1f} "
              f"{s['avg_freshness']:>6.1f} {s['avg_noise']:>6.1f} {s['avg_sources']:>6.1f}  "
              f"{s['free_tier']}{marker}")

    # Win count
    print(f"\n  Wins per provider:")
    win_counts = {}
    for qr in query_results:
        w = qr.get("winner", "")
        win_counts[w] = win_counts.get(w, 0) + 1
    for provider, count in sorted(win_counts.items(), key=lambda x: -x[1]):
        print(f"    {provider}: {count}/{len(query_results)} queries")

    # Cost projection
    print(f"\n  Cost projection (170 searches/day = ~5,100/month):")
    for provider, s in ranked:
        cost_str = s["cost"]
        print(f"    {provider}: {cost_str} — free tier: {s['free_tier']}")

    # Save detailed results
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "benchmark_results.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({
            "date": datetime.now().isoformat(),
            "providers_tested": list(available.keys()),
            "queries": len(queries),
            "summary": summary,
            "ranked": [r[0] for r in ranked],
            "query_details": query_results,
        }, f, indent=2)
    print(f"\n  Detailed results saved to {output_path}")


if __name__ == "__main__":
    if "--full" in sys.argv:
        queries = TEST_QUERIES_FULL
    elif "--query" in sys.argv:
        idx = sys.argv.index("--query")
        queries = [sys.argv[idx + 1]]
    else:
        queries = TEST_QUERIES_QUICK

    run_benchmark(queries)
