# Spec — Brave Search provider

**Status:** Draft
**Owner:** Simon
**Depends on:** the existing `SearchProvider` protocol in [app/search_providers/base.py](../../app/search_providers/base.py) and the fan-out + enrichment hook in [app/search_providers/\_\_init\_\_.py](../../app/search_providers/__init__.py). Requires a small refactor of that hook to remove the hardcoded "Tavily-first" pool ordering — see [Priority & fan-out refactor](#priority--fan-out-refactor). No schema changes, no new DB tables.
**Unblocks:** an independent news index at the head of the pipeline so the scan's dominant viewpoint is no longer Google-shaped; wider source pool for the ranker to dedupe/cluster; cheaper fallback if Tavily or Serper pricing shifts.

## Purpose

Today the scanner fans out to two news indices: **Tavily** (primary, page-content extraction) and **Serper** (Google News passthrough). Both are shaped by Google's index — Serper directly, Tavily largely indirectly — which means when Google deprioritizes or drops a source, we lose it everywhere at once. We've already seen this with a handful of newsroom/blog URLs that are visible in Bing and Brave but absent from Google News entirely.

**Brave Search** maintains its own independent web + news index (not a Google reseller). The Data for AI API is priced for this exact use case — dedicated endpoints for `/news/search` and `/web/search`, documented freshness controls, and meaningful free and paid tiers.

Wiring Brave in as **the highest-priority news provider** — first in the fan-out, first in the merged pool, and the winner on URL collisions — gives the scan a genuinely different centre of gravity. Tavily and Serper keep running (Tavily is still the only provider that ships `raw_content` for page extraction, and it still leads before ZenRows enrichment takes over), but they're demoted to second and third in line. Brave leads.

One new file, one REGISTRY line, plus a targeted refactor of `install_scanner_hook` so pool ordering is driven by config, not hardcoded to Tavily.

## Non-goals

- **Replacing Tavily.** Brave leads, Tavily follows. Tavily still owns page-content extraction (its `raw_content` is the only provider-native full text we get before ZenRows kicks in) and the `topic="general"` deep-search path. "Brave first" is about ordering and dedup, not displacement.
- **Dropping any existing provider.** Tavily and Serper both stay enabled in the default config. Shrinking the provider set is a separate call once we have usage data showing one is redundant.
- **A dedicated Brave web-scope pass.** The scanner today runs `topic="news"` queries through the provider fan-out and `topic="general"` queries through Tavily alone for page extraction. Brave has `/web/search` but adding a web-scope provider means rethinking the fan-out/enrichment contract (who extracts? who dedupes?). Out of scope for v1 — news only. See [What this unblocks](#what-this-unblocks).
- **Changing the enrichment pipeline.** Brave returns snippets + URLs (same shape as Serper). Enrichment via `fetcher.bulk_fetch` (ZenRows → ScrapingBee → trafilatura) already kicks in on every non-Tavily result — Brave inherits that for free.
- **Brave AI Summarizer / Grounding API.** Brave offers a higher-tier endpoint that produces an LLM summary with citations. We already do synthesis ourselves via Claude; paying Brave to do it too is waste. v1 is raw news results only.
- **Localized / multi-region fan-out.** Brave supports `country=AU/US/GB/…` and `search_lang`. v1 uses a single region (configurable per-provider in `config.json`, defaults to `ALL` / English) — no per-competitor region logic.
- **Rate-limit orchestration across providers.** If Brave's free-tier 1 qps is too tight for the scan, the answer is "upgrade the plan" or "disable Brave". We don't build a cross-provider scheduler in v1.
- **Search-result scoring model.** Brave returns results without a relevance score (like Serper). We assign a middle-weight `score=0.5`, same as Serper, and let spec 03's ranker do the real work.

## Design principles

1. **Brave is first in line — by config order, not by hardcoding.** Provider priority is the insertion order of the `search_providers` block in `config.json`. Brave sits at the top of that block. The fan-out builds its pool in that order and URL dedup is first-wins, so Brave's title/snippet/score/`source_provider` label win on any overlap with Tavily or Serper. Reordering the config (now or later) reorders priority — no code change required.
2. **Tavily's "original-function" privilege has to go.** The current hook wraps `scanner.search_tavily` and treats its return as the lead of the pool. That made sense when Tavily was primary; it doesn't now. The hook gets refactored to treat Tavily as just another entry in `news_providers()`, ordered by config (see [Priority & fan-out refactor](#priority--fan-out-refactor)).
3. **One file, mirrors Serper.** The Brave adapter itself is a near-twin of [app/search_providers/serper.py](../../app/search_providers/serper.py) — subclass `SearchProvider`, implement `search_news`, record usage, done. Deliberate duplication over premature abstraction: two snippet providers with slightly different date quirks is fine; three starts to warrant a shared base, but not today.
4. **Registered, not hardcoded.** Added to `REGISTRY` in [app/search_providers/\_\_init\_\_.py](../../app/search_providers/__init__.py) and the default `search_providers` block in `config.json`, enabled by default (matching Serper's default — key-absence is the real gate, so this is safe). User can disable or reorder at `/settings/providers` without a restart.
5. **Same freshness contract as the other news providers.** `days=7` in → `freshness=pw` on the Brave request. Client-side age filtering strips anything Brave returns that's older than the requested window (Brave occasionally returns "3 months ago" results for a `pw` query — same class of problem Serper has, same fix).
6. **Publish-date preference over Brave's `age` string.** Brave returns both a `page_age` (ISO-ish absolute) and a fuzzy `age` ("2 days ago"). Where `page_age` is present, use it verbatim in the `published` field so ranker date logic gets a real datetime, not "2 days ago". Where only `age` is present, parse it to `age_days` (reusing `serper._parse_age_days`) and pass the original string as `published` for display.
7. **Usage tracked per call.** Every Brave call writes a `UsageEvent` row with `provider="brave"`, `cost_usd` computed from a configurable `BRAVE_USD_PER_CALL` (default `0.005` — Data for AI standard tier, revisit after 30 days). `/settings/usage` surfaces it without further work.
8. **Key lives in the same place as every other key.** `BRAVE_API_KEY` added to `MANAGED_KEYS` in [app/env_keys.py](../../app/env_keys.py), same shape as `SERPER_API_KEY`. No module-level key capture (Brave key is read at call time), so no refresh hook needed in `_refresh_module_captures`.
9. **Fail soft — Brave-first must not starve the scan on a Brave outage.** A Brave call that 429s, 5xxs, or times out logs a line, records a `success=False` usage event, and returns `[]`. The rest of the ordered pool — Tavily then Serper — runs as usual. Brave being primary means it leads when it works, not that it's a single point of failure.

## Where it lives

- **Provider**
  - `app/search_providers/brave.py` — new. `BraveProvider` subclass of `SearchProvider`. Implements `available()` and `search_news()`. Private helpers `_freshness(days) -> str | None` and `_record_usage(...)`.
- **Registry**
  - `app/search_providers/__init__.py::REGISTRY` — add `"brave": BraveProvider`.
- **Keys**
  - `app/env_keys.py::MANAGED_KEYS` — add `"BRAVE_API_KEY": "Brave Search (independent news index, optional)"`.
- **Config**
  - `config.json` → `search_providers.brave = {enabled: false, scope: ["news"]}` as the default shape the settings UI expects. (Left disabled; user flips it on post-deploy.)
- **Pricing / usage** *(light touch — just the rate env var)*
  - `BRAVE_USD_PER_CALL` env var (default `"0.005"`) read inside `_record_usage`. No new pricing helper in `app/pricing.py` — the flat per-call rate matches how Serper does it.
- **Settings UI**
  - No template edits. `/settings/providers` already iterates `provider_status(cfg)` which walks `REGISTRY`; Brave shows up automatically. `/settings/keys` already iterates `MANAGED_KEYS`; Brave key row shows up automatically.
- **Tests**
  - `tests/test_brave_provider.py` (if this repo has a tests dir — verify; otherwise the acceptance criteria below are the manual test plan).
- **Hook refactor**
  - `app/search_providers/__init__.py::install_scanner_hook` — rewrite the merge step so the pool is built in config order (see next section). This is a ~20-line change in the wrapped closure; the rest of the hook (signature, scope-check, enrichment block, return) is untouched.
- **No changes to `scanner.py`, `app/jobs.py`, `app/fetcher.py`, or any template.** Pool order changes are invisible to the scanner — it receives a list; the ranker and dedup downstream are order-agnostic except for the stable-sort tie-breakers already in place.

## Priority & fan-out refactor

The current wrapped function in `install_scanner_hook`:

```python
results = original(query, *args, **kwargs)      # Tavily always first
# ... Serper etc. appended to `extras`
for e in extras:
    if e['url'] in seen: continue
    results.append(e)                            # appended — never wins dedup
return results
```

This hardcodes Tavily's position. Replace with a config-ordered build:

```python
# news_providers() already returns providers in config insertion order.
ordered = news_providers()                       # e.g. [brave, tavily, serper]

# Collect every provider's results into a per-name bucket. Tavily is still
# called via `original(...)` so its existing code path (raw_content, usage
# accounting inside search_tavily) is preserved — we just don't prepend it.
buckets: dict[str, list[dict]] = {}
if topic == "news":
    for p in ordered:
        if p.name == "tavily":
            buckets["tavily"] = original(query, *args, **kwargs)
        else:
            try:
                buckets[p.name] = p.search_news(query, max_results=kwargs.get("max_results", 5), days=...)
            except Exception as e:
                print(f"[providers/{p.name}] error: {e}")
                buckets[p.name] = []
else:
    # Non-news topics keep the old shape — Tavily only.
    buckets["tavily"] = original(query, *args, **kwargs)

# Build the pool in priority order. First-seen wins on URL collision, so
# whoever is first in config owns the record.
pool: list[dict] = []
seen: set[str] = set()
for p in ordered:
    for r in buckets.get(p.name, []):
        u = r.get("url") or ""
        if u and u in seen:
            continue
        if u:
            seen.add(u)
        pool.append(r)

# Enrichment block runs over `pool` unchanged.
```

Properties this preserves:

- **Tavily's existing call path is unchanged.** It's still invoked through `original(...)`, which means its own usage tracking, `TAVILY_DAYS` module capture, and `search_depth`/`topic` kwargs all still work. We only change *when* we call it (same turn, no longer first) and *where* its results sit in the merged pool.
- **Non-news topics don't fan out.** `topic="general"` calls (deep search paths used by `app/ranker/` and briefs) bypass the fan-out entirely and get Tavily-only results, same as today.
- **Serper stays valid.** `news_providers()` yields it in config order; it just comes after Brave now.
- **Dedup semantics match intent.** First-wins by URL. With Brave first, same URL returned by Brave + Tavily → Brave's record wins (its `title`, its `score`, `source_provider="brave"`). Subsequent ZenRows enrichment overwrites `content` anyway, so this only matters for the provider label, title wording, and the baseline score before the ranker takes over.

> **Downstream check:** nothing in `scanner.py` or the ranker reads `results[0]` as "the Tavily result" — the pool is iterated and each row carries `source_provider` explicitly. Verify this during implementation; if any code does rely on positional Tavily-first, fix it at the read site, not by reverting pool order.

## Adapter sketch

```python
# app/search_providers/brave.py
"""Brave Search adapter — independent news index via Brave's Data for AI API.
Flat pricing (~$5 per 1000 queries on the standard tier at time of writing);
usage tracked per call. Enable in settings/providers."""
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
    description = "Brave Search — independent news index (not a Google reseller). Use for source diversity beyond Tavily/Serper."
    env_var = "BRAVE_API_KEY"

    # Brave caps count at 20 per request for news
    _MAX_COUNT = 20

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
            # Single region for v1; future spec can per-competitor region.
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

        raw_results = (data.get("results") or data.get("news", {}).get("results") or [])
        self._record_usage(success=True, result_count=len(raw_results))

        out = []
        stale_dropped = 0
        for r in raw_results:
            title = r.get("title") or ""
            snippet = r.get("description") or ""
            href = r.get("url") or ""
            if not href:
                continue

            # Prefer absolute ISO date if present, else fuzzy age string.
            page_age = r.get("page_age") or r.get("meta_url", {}).get("page_age") or ""
            age_str = r.get("age") or ""
            age_days = _parse_age_days(age_str) if age_str else None
            published_display = page_age or age_str

            # Hard cutoff: drop anything older than the requested window,
            # matching the stale-drop behavior we wired for Serper. If age
            # is unparseable, keep it (can't tell — better to include).
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
                "source_name": (r.get("meta_url") or {}).get("hostname") or "",
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
```

> **API shape caveat.** Brave has iterated the `/news/search` response shape a couple of times; treat the top-level key pick (`results` vs `news.results`) in the adapter as defensive — whichever one is populated wins. Verify against a live response on first wire-up; adjust the pick if Brave's current shape is different.

## Execution flow

1. Admin pastes the key at `/settings/keys` → `set_key("BRAVE_API_KEY", ...)` writes `.env` + `os.environ`. No module capture to refresh.
2. Brave ships `enabled: true` in the default config block (first entry). On first boot after upgrade with the key set, `load_from_config` adds Brave to `_active` at position 0. With the key unset, Brave is skipped with a "missing key — disabled" log; Tavily + Serper run as today.
3. Next scan: `scanner.search_tavily` is called with `topic="news"`. The refactored wrapper in `install_scanner_hook`:
   - iterates `news_providers()` in config order — Brave first, Tavily second, Serper third,
   - calls `BraveProvider().search_news(query, max_results=N, days=D)` first,
   - calls Tavily via `original(...)` (same call path as today),
   - calls Serper,
   - builds the pool in that order, keeping first-seen per URL (Brave wins on overlap),
   - enriches every result via `fetcher.bulk_fetch` (unchanged),
   - replaces `published` with the extracted page date when available (unchanged).
4. Each Brave call writes one `UsageEvent`. `/settings/usage` shows a new "brave" row, typically with the highest call count of the three news providers since it leads the fan-out. `/runs` tracks it via `current_run_id` context.
5. On any Brave error (401, 429, timeout, JSON parse), the adapter logs, records `success=False`, returns `[]`. The pool is built from Tavily + Serper as a natural fallback — no retry, no cascading failure.

## Config

Added to `config.json` with **Brave listed first** — insertion order is the priority ranking:

```json
"search_providers": {
  "brave":  { "enabled": true, "scope": ["news"] },
  "tavily": { "enabled": true, "scope": ["news"] },
  "serper": { "enabled": true, "scope": ["news"] }
}
```

On deploy of this spec, the migration step for existing installs is a single `config.json` edit: reorder the `search_providers` block so Brave is the first key and add the new entry. If an operator wants to change priority later (e.g., temporarily demote Brave while investigating index quality), they reorder the block via a direct config edit today; a future settings-UI reorder control is a natural follow-up but out of scope here.

Optional env tunables (defaults are fine; documented in `.env.example` if that file exists):

- `BRAVE_API_KEY` — required to enable.
- `BRAVE_USD_PER_CALL` — default `0.005`. Align with the current tier before billing surprises anyone.
- `BRAVE_COUNTRY` — default `ALL`. ISO country code. One global setting for v1.
- `BRAVE_LANG` — default `en`.

## Auth & permissions

- Settings routes already require `admin` role (`require_role("admin")`) for both provider toggle and key set. No change.
- No new end-user-facing endpoint — provider is invoked from the scanner via the existing fan-out hook.

## Observability

- `/settings/providers` row for Brave shows: description, key-present state, enabled state, scope, `available()` result. No new fields.
- `/settings/usage` surfaces Brave spend via the existing provider group-by. Confirm `provider="brave"` is rendered without special-casing (the usage page groups by the column; it will show up automatically).
- `/runs` shows Brave calls under the run's usage totals via the existing `UsageEvent.run_id` FK and the `current_run_id` contextvar (set by the scan job wrapper).
- Stale-drop logs print to stdout with the `[brave]` prefix, matching the `[serper]` pattern.

## Error handling

| Case                                      | Behavior                                                                                     |
| ----------------------------------------- | -------------------------------------------------------------------------------------------- |
| Missing key (`BRAVE_API_KEY` unset)       | `available()` returns `False`; `load_from_config` logs "disabled — missing key"; never called. |
| 401 / 403                                 | Logged, `success=False` usage event, returns `[]`. Other providers unaffected.               |
| 429 rate limit                            | Same as above. v1 has no retry — the per-query cadence from the scan is low enough that 429s indicate a plan-tier mismatch, not transient load. |
| 5xx / timeout                             | Same as above.                                                                               |
| Empty `results`                           | Returns `[]`, `success=True`, `result_count=0`.                                              |
| Result missing `url`                      | Skipped silently (Tavily/Serper both do the same).                                           |
| All results stale beyond `days` window    | Dropped; log line counts it; returns `[]`. Scan proceeds with Tavily + Serper only.          |

## Testing

Manual acceptance, ordered to catch the most common wire-up mistakes first:

1. Deploy with no `BRAVE_API_KEY`. `/settings/providers` shows Brave row first, `available=false`. Toggling enabled is allowed (persists) but `load_from_config` logs "disabled — missing key" and excludes it from `_active`. Scan still works via Tavily + Serper.
2. Paste a valid key at `/settings/keys`. Refresh `/settings/providers` — `available=true`. Confirm startup log now shows `[providers] enabled 'brave' for scope=['news']` **before** the tavily/serper enable lines (order = priority).
3. Kick off a scan for a seeded competitor. Tail stdout — expect the Brave call logs *before* the Tavily and Serper calls for each query.
4. Inspect the resulting `Findings` for that scan: where a URL was returned by Brave AND Tavily, `source_provider="brave"` wins. (Query the DB or add a temporary debug log in the hook.)
5. `/settings/usage` shows a row with `provider="brave"`, `operation="news"`, nonzero `cost_usd`. Brave's call count per scan is equal to the number of unique news queries — same as Tavily's — confirming it isn't being short-circuited.
6. `/runs/<latest>` shows Brave calls contributing to the run's total cost.
7. Open a finding whose `source_provider="brave"`. Confirm `enriched=true` (ZenRows took over) and body text is substantial, not just the Brave snippet.
8. Force a stale-window case: temporarily set `limits.recency_days_brief=1` in config and run a quieter competitor. Confirm Brave's `[brave] dropped N/M stale (older than 1d) — freshness=pd` log line appears.
9. **Fail-soft on Brave outage.** Paste an invalid key, rerun scan. Confirm `[brave] Error: …` logged, a `success=False` usage event, `cost_usd=0.0`, and that Tavily + Serper results still reach the ranker — scan coverage degrades but does not collapse.
10. **Config-order priority is the only lever.** Edit `config.json` to swap Brave and Tavily positions, reload. Rerun scan. Tavily now leads; on URL overlap, `source_provider="tavily"` wins. Restore Brave-first after verifying.
11. Toggle Brave **off** at `/settings/providers`. Next scan: no `[brave]` lines in stdout, no new `brave` rows in `usage_events`; Tavily + Serper pool reverts to today's shape.
12. `/settings/keys` row for Brave shows masked hint (e.g. `BSAf…xyz`), `set=true`; delete key, row reverts to `set=false`; provider auto-drops from `_active` on next `load_from_config` call.

## Acceptance criteria

1. `BraveProvider` class in `app/search_providers/brave.py` implements the `SearchProvider` protocol and returns results in the existing scanner shape.
2. `BRAVE_API_KEY` is listed on `/settings/keys` with a working set/clear flow.
3. `/settings/providers` shows a Brave row (first in the list); toggling it persists to `config.json` and re-registers providers live, no restart.
4. **Priority:** with Brave enabled and key set, Brave is called **first** in the news fan-out for every query, and the merged pool places Brave results ahead of Tavily and Serper. URL-collision dedup is first-wins, so a URL returned by Brave + Tavily emerges from the hook with `source_provider="brave"`.
5. **Config order = priority.** Reordering the `search_providers` block in `config.json` changes the fan-out order and dedup winner without any code change.
6. **Tavily's existing contract is preserved.** Non-news (`topic="general"`) calls still go Tavily-only; `topic="news"` Tavily calls still produce the same `raw_content`/`usage` they do today — only their position in the merged pool changes.
7. Each Brave call writes exactly one `UsageEvent` row with `provider="brave"`, correct `cost_usd`, and `run_id` populated from the current scan.
8. Freshness: `days` window from the scan maps to Brave `freshness=pd|pw|pm|py`; results older than the window are dropped client-side with a stdout log.
9. Absolute `page_age` (when Brave provides it) is preferred over the fuzzy `age` string for the `published` field on each result.
10. **Fail-soft under Brave outage:** errors (missing key, 401/429/5xx, timeouts, JSON parse) never raise into the scanner; they log, record `success=False`, return `[]`; Tavily + Serper continue producing results and the scan does not collapse.
11. No changes required to `scanner.py`, `app/jobs.py`, `app/fetcher.py`, templates, or CSS beyond the hook refactor in `app/search_providers/__init__.py`.
12. Defaults ship with Brave `enabled=true` and listed first — on an install where `BRAVE_API_KEY` is unset, this is a no-op (key-absence gate); on an install where the key is set, Brave is live immediately.

## What this unblocks

- **A true web-scope provider pass.** Once Brave is known-good on news, a follow-up spec can wire `topic="web"` (or a new `topic="deep_web"`) through a Brave web search to surface non-news pages (docs, pricing pages, status pages, support forums) beyond what Tavily's advanced search finds.
- **Regional fan-out.** `BRAVE_COUNTRY` becomes a per-competitor field. ANZ-focused competitors get `country=AU`; US-focused competitors get `country=US`. Improves signal for the geography we actually care about.
- **Provider A/B.** With three independent indices, we can measure which combinations meaningfully change the ranker's top-20. Candidate: drop Serper on quarters where Brave+Tavily cover it at lower cost.
- **Failover discipline.** The scanner today degrades silently if Tavily has a bad hour. Three providers + basic coverage metrics in `/settings/usage` means we'll notice when one of them is drifting — and have two others to lean on while we investigate.
- **Grounding for Deep Research citations cross-check.** The Deep Research tab (spec 04) shows Gemini's citations. A future analytics pass could compare Gemini's cited domains against what Brave + Tavily + Serper are returning for the same competitor — surfaces "Gemini found something our scan never sees" gaps.
