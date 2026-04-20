"""
Competitor Manager — discovers new competitors and prunes stale ones.

Two jobs:
  1. DISCOVERY — periodic "horizon scan" for emerging competitors
     Uses Claude to speculate on who might be entering the space,
     then validates with a Tavily search. If evidence passes a threshold,
     the competitor gets added to the watchlist on probation.

  2. PRUNING — tracks activity per competitor across scans.
     If a competitor has no meaningful findings for N days (default 60),
     it gets flagged for removal. Core competitors (manually added)
     are never auto-removed.

State is stored in data/competitor_state.json alongside the main memory.
"""

import os
import json
from datetime import datetime, timedelta

import anthropic

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
STATE_FILE = os.path.join(DATA_DIR, "competitor_state.json")
CONFIG_PATH = os.environ.get("CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))

client = anthropic.Anthropic()
MODEL = "claude-sonnet-4-6"
FAST_MODEL = "claude-haiku-4-5-20251001"

# ═══════════════════════════════════════════════════════════════
#  STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def load_state() -> dict:
    """Load competitor tracking state."""
    defaults = {
        "competitor_activity": {},   # name -> {last_meaningful, finding_count, added_date, source}
        "discovery_history": [],     # [{date, candidates, added, rejected}]
        "pruned": [],                # [{name, date, reason, finding_count}]
        "last_discovery": None,
        "last_prune_check": None,
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                loaded = json.load(f)
            return {**defaults, **loaded}
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [state] WARNING: could not load {STATE_FILE} ({e}); using defaults")
    return defaults


def save_state(state: dict):
    """Save competitor tracking state."""
    os.makedirs(DATA_DIR, exist_ok=True)
    # Cap history
    state["discovery_history"] = state["discovery_history"][-20:]
    state["pruned"] = state["pruned"][-50:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


# ═══════════════════════════════════════════════════════════════
#  ACTIVITY TRACKING — called after each scan
# ═══════════════════════════════════════════════════════════════

def record_scan_activity(findings: list[dict], config: dict):
    """Record which competitors had meaningful findings in this scan.
    Call this after every scan to keep activity tracking current.
    """
    state = load_state()
    activity = state.get("competitor_activity", {})
    today = datetime.now().strftime("%Y-%m-%d")

    # Ensure all current competitors are tracked
    for comp in config["competitors"]:
        name = comp["name"]
        if name not in activity:
            activity[name] = {
                "last_meaningful": today,
                "finding_count": 0,
                "total_scans": 0,
                "added_date": today,
                "source": comp.get("_source", "manual"),  # "manual" or "discovered"
            }
        activity[name]["total_scans"] = activity[name].get("total_scans", 0) + 1

    # Count meaningful findings per competitor
    comp_counts = {}
    for f in findings:
        comp_name = f.get("competitor", "")
        if comp_name:
            comp_counts[comp_name] = comp_counts.get(comp_name, 0) + 1

    for name, count in comp_counts.items():
        if name in activity and count > 0:
            activity[name]["last_meaningful"] = today
            activity[name]["finding_count"] = activity[name].get("finding_count", 0) + count

    state["competitor_activity"] = activity
    save_state(state)


# ═══════════════════════════════════════════════════════════════
#  DISCOVERY — find new competitors
# ═══════════════════════════════════════════════════════════════

DISCOVERY_INTERVAL_DAYS = 7  # run discovery once a week


def should_run_discovery(state: dict) -> bool:
    """Check if it's time for a discovery sweep."""
    last = state.get("last_discovery")
    if not last:
        return True
    try:
        last_date = datetime.fromisoformat(last)
        return (datetime.now() - last_date).days >= DISCOVERY_INTERVAL_DAYS
    except (ValueError, TypeError):
        return True


def discover_new_competitors(config: dict) -> list[dict]:
    """Use Claude to speculate on emerging competitors, then validate with search.

    Returns list of candidates: [{name, evidence, relevance, recommendation}]
    """
    from scanner import search_tavily
    from app.adapters.fetch.sanitize import EXCLUDE_DOMAINS

    state = load_state()
    existing = [c["name"] for c in config["competitors"]]
    pruned_names = [p["name"] for p in state.get("pruned", [])]
    company = config["company"]
    industry = config.get("industry", "job search and recruitment")

    # Step 1: Ask Claude to speculate on emerging competitors
    print("  [discovery] Asking Claude to identify potential new competitors...")

    speculation_prompt = f"""You are a competitive intelligence analyst for {company} in the {industry} industry.

Current competitors being tracked: {', '.join(existing)}
Previously tracked and dropped (low relevance): {', '.join(pruned_names) if pruned_names else 'None'}

Identify 3-5 companies or products that could be emerging competitors to {company} but are NOT in the current list. Think about:

1. **AI-native startups** disrupting traditional job boards (e.g., AI matching, conversational job search)
2. **Adjacent players** expanding into recruitment (e.g., social platforms, HR tech, freelance platforms)
3. **Regional players** gaining traction in markets where {company} operates (ANZ, Asia, Latin America)
4. **New entrants** with recent funding, product launches, or market entry announcements

For each candidate, provide:
- Company name (exact, as they brand themselves)
- Why they could be a threat to {company}
- What to search for to validate

Return ONLY valid JSON as a list:
[
  {{"name": "CompanyName", "reason": "why they matter", "search_queries": ["query1", "query2"]}}
]

Be specific and current. Don't suggest companies that are already in the tracked list."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": speculation_prompt}],
        )
        resp_text = response.content[0].text.strip()
        if resp_text.startswith("```"):
            resp_text = resp_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        candidates = json.loads(resp_text)
    except Exception as e:
        print(f"  [discovery] Failed to get candidates from Claude: {e}")
        return []

    # Step 2: Validate each candidate with Tavily search
    validated = []
    for candidate in candidates[:5]:
        name = candidate.get("name", "")
        if not name or name in existing:
            continue

        print(f"  [discovery] Validating candidate: {name}")
        evidence = []
        queries = candidate.get("search_queries", [f"{name} recruitment jobs platform"])

        for query in queries[:2]:
            results = search_tavily(
                query,
                search_depth="advanced",
                max_results=3,
                exclude_domains=EXCLUDE_DOMAINS,
                include_raw=False,
            )
            for r in results:
                if r.get("content") and len(r["content"]) > 50:
                    evidence.append({
                        "title": r.get("title", ""),
                        "snippet": r["content"][:300],
                        "url": r.get("url", ""),
                        "score": r.get("score", 0),
                    })

        if not evidence:
            print(f"  [discovery] No evidence found for {name} — skipping")
            continue

        # Step 3: Ask Claude to evaluate the evidence
        eval_prompt = f"""Based on this evidence, should {company} start tracking "{name}" as a competitor?

Reason suggested: {candidate.get('reason', 'N/A')}

Evidence found:
{json.dumps(evidence[:5], indent=2)}

Rate on a scale of 1-10:
- 1-3: Not relevant, ignore
- 4-6: Mildly interesting but not a direct competitor yet
- 7-8: Worth tracking on probation
- 9-10: Definite competitor, should have been tracked already

Return ONLY valid JSON:
{{"score": N, "recommendation": "add" or "skip", "reason": "one line explanation", "keywords": ["search keyword 1", "search keyword 2"]}}"""

        try:
            eval_response = client.messages.create(
                model=FAST_MODEL,
                max_tokens=200,
                messages=[{"role": "user", "content": eval_prompt}],
            )
            eval_text = eval_response.content[0].text.strip()
            if eval_text.startswith("```"):
                eval_text = eval_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            evaluation = json.loads(eval_text)
        except Exception:
            evaluation = {"score": 0, "recommendation": "skip", "reason": "Failed to evaluate"}

        validated.append({
            "name": name,
            "reason": candidate.get("reason", ""),
            "evidence_count": len(evidence),
            "score": evaluation.get("score", 0),
            "recommendation": evaluation.get("recommendation", "skip"),
            "eval_reason": evaluation.get("reason", ""),
            "keywords": evaluation.get("keywords", [name]),
        })

        print(f"  [discovery] {name}: score={evaluation.get('score', 0)}/10 → {evaluation.get('recommendation', 'skip')}")

    return validated


def add_discovered_competitor(name: str, keywords: list[str], reason: str, config: dict) -> dict:
    """Add a new competitor to the config on probation."""
    new_comp = {
        "name": name,
        "keywords": keywords if keywords else [name],
        "subreddits": ["recruiting", "jobs"],
        "careers_domains": [],
        "careers_query": f"{name} careers hiring engineering product",
        "_source": "discovered",
        "_discovered_date": datetime.now().strftime("%Y-%m-%d"),
        "_discovery_reason": reason,
    }
    config["competitors"].append(new_comp)
    save_config(config)
    return new_comp


def run_discovery(config: dict) -> dict:
    """Run the full discovery pipeline. Returns summary of actions taken."""
    state = load_state()

    if not should_run_discovery(state):
        days_since = "unknown"
        if state.get("last_discovery"):
            try:
                days_since = (datetime.now() - datetime.fromisoformat(state["last_discovery"])).days
            except (ValueError, TypeError):
                pass
        print(f"  [discovery] Skipping — last run {days_since} days ago (interval: {DISCOVERY_INTERVAL_DAYS} days)")
        return {"skipped": True, "reason": "too_soon"}

    print(f"\n  [discovery] Running new competitor sweep...")
    candidates = discover_new_competitors(config)

    added = []
    rejected = []
    threshold = 7  # minimum score to auto-add

    for candidate in candidates:
        if candidate["score"] >= threshold and candidate["recommendation"] == "add":
            comp = add_discovered_competitor(
                candidate["name"],
                candidate.get("keywords", []),
                candidate.get("eval_reason", candidate.get("reason", "")),
                config,
            )
            added.append(candidate["name"])
            print(f"  [discovery] ✓ Added: {candidate['name']} (score: {candidate['score']}/10)")

            # Initialize activity tracking
            state.setdefault("competitor_activity", {})[candidate["name"]] = {
                "last_meaningful": datetime.now().strftime("%Y-%m-%d"),
                "finding_count": 0,
                "total_scans": 0,
                "added_date": datetime.now().strftime("%Y-%m-%d"),
                "source": "discovered",
            }
        else:
            rejected.append({"name": candidate["name"], "score": candidate["score"], "reason": candidate.get("eval_reason", "")})
            print(f"  [discovery] ✗ Skipped: {candidate['name']} (score: {candidate['score']}/10)")

    # Record history
    state["discovery_history"].append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "candidates_evaluated": len(candidates),
        "added": added,
        "rejected": [r["name"] for r in rejected],
    })
    state["last_discovery"] = datetime.now().isoformat()
    save_state(state)

    summary = {
        "candidates_found": len(candidates),
        "added": added,
        "rejected": rejected,
    }
    if added:
        print(f"  [discovery] Added {len(added)} new competitors: {', '.join(added)}")
    else:
        print(f"  [discovery] No new competitors met the threshold (score >= {threshold}/10)")

    return summary


# ═══════════════════════════════════════════════════════════════
#  PRUNING — drop stale competitors
# ═══════════════════════════════════════════════════════════════

STALE_THRESHOLD_DAYS = 60       # no meaningful findings for this many days
MIN_SCANS_BEFORE_PRUNE = 8     # must have been scanned at least this many times
PRUNE_CHECK_INTERVAL_DAYS = 7  # check for stale competitors weekly


def should_run_prune(state: dict) -> bool:
    """Check if it's time for a prune check."""
    last = state.get("last_prune_check")
    if not last:
        return True
    try:
        last_date = datetime.fromisoformat(last)
        return (datetime.now() - last_date).days >= PRUNE_CHECK_INTERVAL_DAYS
    except (ValueError, TypeError):
        return True


def check_for_stale_competitors(config: dict) -> list[dict]:
    """Identify competitors that should be considered for removal.

    Rules:
    - Only auto-discovered competitors can be pruned (manual ones are flagged but kept)
    - Must have been tracked for at least STALE_THRESHOLD_DAYS
    - Must have had at least MIN_SCANS_BEFORE_PRUNE scans
    - No meaningful findings in STALE_THRESHOLD_DAYS
    """
    state = load_state()
    activity = state.get("competitor_activity", {})
    today = datetime.now()
    stale = []

    for comp in config["competitors"]:
        name = comp["name"]
        tracker = activity.get(name, {})

        if not tracker:
            continue

        # Check if enough time and scans have passed
        added_date_str = tracker.get("added_date", today.strftime("%Y-%m-%d"))
        try:
            added_date = datetime.strptime(added_date_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue

        days_tracked = (today - added_date).days
        if days_tracked < STALE_THRESHOLD_DAYS:
            continue

        total_scans = tracker.get("total_scans", 0)
        if total_scans < MIN_SCANS_BEFORE_PRUNE:
            continue

        # Check last meaningful activity
        last_meaningful_str = tracker.get("last_meaningful", added_date_str)
        try:
            last_meaningful = datetime.strptime(last_meaningful_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue

        days_since_activity = (today - last_meaningful).days
        if days_since_activity >= STALE_THRESHOLD_DAYS:
            source = tracker.get("source", comp.get("_source", "manual"))
            stale.append({
                "name": name,
                "days_since_activity": days_since_activity,
                "total_findings": tracker.get("finding_count", 0),
                "total_scans": total_scans,
                "source": source,
                "can_auto_prune": source == "discovered",
            })

    return stale


def prune_competitors(config: dict) -> dict:
    """Run the prune check and remove stale discovered competitors.
    Manual competitors are flagged in the report but not removed.
    """
    state = load_state()

    if not should_run_prune(state):
        return {"skipped": True}

    stale = check_for_stale_competitors(config)
    if not stale:
        state["last_prune_check"] = datetime.now().isoformat()
        save_state(state)
        print(f"  [prune] All competitors are active")
        return {"pruned": [], "flagged": []}

    pruned = []
    flagged = []

    for entry in stale:
        if entry["can_auto_prune"]:
            # Remove from config
            config["competitors"] = [
                c for c in config["competitors"] if c["name"] != entry["name"]
            ]
            # Remove from activity tracking
            state.get("competitor_activity", {}).pop(entry["name"], None)
            # Record in pruned history
            state.setdefault("pruned", []).append({
                "name": entry["name"],
                "date": datetime.now().strftime("%Y-%m-%d"),
                "reason": f"No meaningful findings in {entry['days_since_activity']} days "
                         f"({entry['total_findings']} total findings over {entry['total_scans']} scans)",
                "finding_count": entry["total_findings"],
            })
            pruned.append(entry["name"])
            print(f"  [prune] Removed: {entry['name']} (no activity in {entry['days_since_activity']} days)")
        else:
            flagged.append(entry)
            print(f"  [prune] Flagged (manual, not auto-removed): {entry['name']} "
                  f"(no activity in {entry['days_since_activity']} days)")

    if pruned:
        save_config(config)

    state["last_prune_check"] = datetime.now().isoformat()
    save_state(state)

    return {"pruned": pruned, "flagged": flagged}


# ═══════════════════════════════════════════════════════════════
#  STATUS REPORT — for inclusion in digest
# ═══════════════════════════════════════════════════════════════

def get_watchlist_status(config: dict) -> str:
    """Generate a status summary of the competitor watchlist for inclusion in digests."""
    state = load_state()
    activity = state.get("competitor_activity", {})
    lines = []

    # New additions
    recent_history = state.get("discovery_history", [])[-3:]
    recent_adds = []
    for h in recent_history:
        for name in h.get("added", []):
            recent_adds.append(f"{name} (added {h['date']})")
    if recent_adds:
        lines.append(f"**Recently added:** {', '.join(recent_adds)}")

    # Recently pruned
    recent_pruned = [p for p in state.get("pruned", [])
                     if _days_ago(p.get("date", "")) < 30]
    if recent_pruned:
        names = [f"{p['name']} ({p.get('reason', 'stale')})" for p in recent_pruned]
        lines.append(f"**Recently dropped:** {', '.join(names)}")

    # Stale warnings (approaching threshold)
    today = datetime.now()
    warnings = []
    for comp in config["competitors"]:
        tracker = activity.get(comp["name"], {})
        last_str = tracker.get("last_meaningful", "")
        if last_str:
            try:
                last = datetime.strptime(last_str, "%Y-%m-%d")
                days = (today - last).days
                if days >= 45 and days < STALE_THRESHOLD_DAYS:  # warn 15 days before prune
                    source = tracker.get("source", comp.get("_source", "manual"))
                    tag = " (auto-discovered)" if source == "discovered" else ""
                    warnings.append(f"{comp['name']}{tag}: {days} days without meaningful news")
            except (ValueError, TypeError):
                pass
    if warnings:
        lines.append(f"**Approaching stale threshold ({STALE_THRESHOLD_DAYS} days):** {'; '.join(warnings)}")

    # On-probation competitors
    probation = [c["name"] for c in config["competitors"] if c.get("_source") == "discovered"]
    if probation:
        lines.append(f"**On probation (auto-discovered):** {', '.join(probation)}")

    return "\n".join(lines) if lines else ""


def _days_ago(date_str: str) -> int:
    """Helper: how many days ago was this date string."""
    try:
        return (datetime.now() - datetime.strptime(date_str, "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return 999


# ═══════════════════════════════════════════════════════════════
#  CLI — test standalone
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    config = load_config()
    print(f"Current competitors: {', '.join(c['name'] for c in config['competitors'])}")

    if "--discover" in sys.argv:
        print("\nRunning discovery (ignoring interval)...")
        state = load_state()
        state["last_discovery"] = None  # force run
        save_state(state)
        result = run_discovery(config)
        print(f"\nResult: {json.dumps(result, indent=2)}")
    elif "--prune" in sys.argv:
        print("\nChecking for stale competitors...")
        stale = check_for_stale_competitors(config)
        if stale:
            for s in stale:
                print(f"  {s['name']}: {s['days_since_activity']} days inactive, "
                      f"{s['total_findings']} findings, auto-prune={s['can_auto_prune']}")
        else:
            print("  All competitors are active")
    elif "--status" in sys.argv:
        status = get_watchlist_status(config)
        print(f"\nWatchlist status:\n{status or '(all clear)'}")
    else:
        print("\nUsage:")
        print("  python competitor_manager.py --discover   Run discovery sweep")
        print("  python competitor_manager.py --prune      Check for stale competitors")
        print("  python competitor_manager.py --status     Show watchlist status")
