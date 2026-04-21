# Spec 03 — Positioning pillars tab

**Status:** Draft
**Owner:** Simon
**Depends on:** 02 (tabbed competitor profile — this spec adds a fourth tab using the same `[hidden]`/hash-driven system).
**Unblocks:** cross-competitor landscape view (`/positioning`, a future spec) that reads from the same `PositioningSnapshot` table; homepage-diff-driven `messaging_shift` findings auto-filed when pillars change between snapshots.

## Purpose

The Review tab tells you what a competitor *did* over the last 4–6 weeks. It doesn't tell you what they *stand for* today, or how that has shifted over the last 6–12 months. Positioning lives on marketing surfaces (homepage, pricing, product pages), not in news and careers signals — so the scanner never touches it, and the review LLM only sees it incidentally when Tavily happens to return a marketing page.

Extract positioning **pillars** from the competitor's own marketing pages on a monthly cadence, store each extraction as an append-only snapshot, and render them as a new **Positioning** tab on the competitor profile. Pillars are the 3–6 things the competitor's own homepage is trying to convince you they are — "AI-native", "enterprise-grade", "developer-first", "fastest in Asia". Snapshots over time show strategy drift — the single highest-leverage comparative signal we don't currently capture.

## Non-goals

- **Cross-competitor landscape / pillar matrix.** That needs multiple competitors with fresh snapshots and its own page. Future spec.
- **Continuous monitoring.** Positioning doesn't move weekly. Monthly cadence + manual refresh. Do not tie extraction to the per-scan loop.
- **Inferring pillars from findings.** Pillars come from what competitors say about themselves on their own marketing pages, not what news sites say about them. Findings-based positioning inference is a different, noisier signal.
- **Image / OG / screenshot analysis.** Text only for v1. If a competitor's positioning is entirely in a hero video or a screenshot, we accept we'll miss it.
- **Full site crawl.** A bounded list of pages per competitor (homepage + optional pricing + optional product). No spidering.
- **Per-pillar time-series charts.** Pillar names aren't stable identifiers across snapshots — "AI-powered" in March and "AI-native" in June are the same story but different strings. The diff narrative covers this; charting doesn't.
- **Auto-filing `messaging_shift` findings.** The hook exists (snapshot diff is a natural trigger) but wire-up is a follow-up, not v1.
- **Editing positioning page URLs from the UI.** Config-level for now; admin edit screen can expose it in a later pass.

## Design principles

1. **Append-only, like `CompetitorReport`.** One row per extraction. Latest = current view; older rows = history. No updates, no deletes in the happy path. The `CompetitorReport` shape is the template; steal it.
2. **Separate pipeline from the scan loop.** Positioning extraction is a standalone job (`app/signals/positioning.py::extract_positioning`) callable from: the scheduler (monthly cron), the admin "Refresh positioning" button, and a backfill script. The per-scan `Run` does not trigger it. The two loops have completely different cadences and failure modes — don't entangle them.
3. **Haiku for extraction, not Sonnet.** The task is structured extraction from ~5–20KB of marketing copy, not synthesis. Haiku 4.5 with a skill prompt is the right tool; Sonnet is only worth it if we later add the cross-competitor landscape pass.
4. **Two skills, not one.** Positioning has two distinct jobs: (a) extract pillars from marketing pages into JSON; (b) write a narrative synthesising current positioning and what changed vs. prior. Extraction is a schema contract — you rarely tune it. Narrative is tone / structure / "what counts as material change" — you tune it often. Keeping them separate means editing narrative prose can't break JSON extraction. Mirrors how `competitor_review` is a standalone skill rather than bundled into `market_digest`.
5. **Both skills go through `app/skills.py`.** Each has a file under `skill/` *and* a row in `KNOWN_SKILLS` so they're seeded into the `skills` table on boot, editable at `/settings/skills`, versioned, and restorable. Same pattern as every other skill. Callers use `skills.load_active("positioning_extract")` / `load_active("positioning_narrative")` — never import file paths.
6. **Two calls per extraction.** Call 1: marketing text → pillars JSON. Call 2: pillars + prior pillars → narrative markdown. The output of call 1 is the input to call 2. Costs ~2× one combined call, still small absolute dollars. The contract between them is the pillars JSON schema — if that's stable, each prompt evolves independently.
7. **Pillars are a structured JSON payload, body is markdown.** Tab UI renders pillar cards from the JSON; the body rendering uses the markdown. Both are stored on the same snapshot row.
8. **Fetch through the existing fetcher.** `app/fetcher.py` already handles ZenRows / ScrapingBee / urllib fallback, sanitization, and bot-wall detection. Positioning pages are no different from any other URL. Reuse it.
9. **Fail soft, per call.** If the fetch fails, skip the extraction for that competitor this cycle. If call 1 returns malformed JSON, log and skip — no snapshot written. If call 2 fails after call 1 succeeded, write the snapshot with `body_md = ""` and a warning logged; pillars alone are still useful and the narrative can be regenerated from the stored pillars without refetching.

## Where it lives

- **Model + migration**
  - `app/models.py` — new `PositioningSnapshot` class.
  - `alembic/versions/<new>_positioning_snapshots.py` — create the table.
- **Extraction pipeline**
  - `app/signals/positioning.py` — new module. `extract_positioning(competitor, db)` is the entry point. Internally calls `_extract_pillars(...)` (call 1) and `_write_narrative(...)` (call 2).
  - `skill/positioning_extract.md` — extraction prompt (marketing pages → pillars JSON).
  - `skill/positioning_narrative.md` — narrative prompt (pillars + prior pillars → markdown body).
  - `app/skills.py::KNOWN_SKILLS` — two new entries registering both skills with the loader so they appear in `/settings/skills`, get seeded on boot, and are versioned in the `skills` table. Callers use `load_active("positioning_extract")` and `load_active("positioning_narrative")`.
- **Scheduler**
  - `app/scheduler.py` — add a monthly job that iterates active competitors and calls `extract_positioning` for each. Rate-limit-friendly: 1 competitor per minute, not a stampede.
- **Routes**
  - `app/routes/competitors.py` — the existing competitor profile loader gains the latest `PositioningSnapshot` + prior snapshot list, the same way it already loads `CompetitorReport` + history.
  - `app/routes/competitors.py` — new `POST /competitors/{id}/positioning/refresh` endpoint that enqueues/runs a single extraction and returns 303 to the profile. Auth-gated (admin or analyst role — match the existing "Regenerate review" endpoint).
- **Template**
  - `app/templates/competitor_profile.html` — add `Positioning` tab button, add `#tab-positioning` panel wrapping the pillar-card grid + "what changed" section + prior-snapshot history.
  - `app/templates/_positioning_pillars.html` — new partial rendering the pillar cards from the JSON payload. Kept separate so the future landscape view can include it too.
- **CSS**
  - `app/static/style.css` — `.pillar-grid`, `.pillar-card`, `.pillar-weight`, `.pillar-quote`. Match the existing `.panel` / `.signal-card` visual language.
- **No new JS dependencies, no new search providers, no new LLM SDKs.**

## Data model

### `PositioningSnapshot`

```python
class PositioningSnapshot(Base):
    """One extraction of a competitor's marketing-page positioning.
    Append-only: latest per competitor_id is the 'current' view, older rows
    are history surfaced as a collapsible list on the Positioning tab.

    Not tied to a scan Run — positioning extraction has its own monthly
    cadence and manual refresh button. run_id is NULL.
    """
    __tablename__ = "positioning_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)

    # The structured pillars payload the tab renders as cards. Schema:
    #   [{"name": str, "weight": 1..5, "quote": str, "source_url": str}]
    # 3–6 items by convention; LLM prompt enforces.
    pillars: Mapped[list] = mapped_column(JSON, default=list)

    # Narrative markdown. Sections:
    #   ## Current positioning        (2–3 sentences synthesising the pillars)
    #   ## What changed since {date}  (diff vs. prior snapshot, or "First snapshot.")
    #   ## Evidence                   (per-pillar supporting quotes with source URLs)
    # Rendered via marked.js in the tab, same as the Review tab.
    body_md: Mapped[str] = mapped_column(Text)

    # URLs actually fetched for this extraction. Not just the configured list —
    # the subset that came back non-empty. Stored so a future "refetch this page"
    # action has something concrete to target.
    source_urls: Mapped[list] = mapped_column(JSON, default=list)

    # SHA-256 of the concatenated fetched text. If it matches the prior
    # snapshot's hash we skip the LLM call entirely and just link the existing
    # snapshot as the current view (no new row). Cheap no-op detection.
    source_hash: Mapped[str] = mapped_column(String(64), index=True)

    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
```

**Index:** one covering index on `(competitor_id, created_at DESC)` — both the "latest" lookup and the history list use it.

**Why not reuse `CompetitorReport` with a new `kind` column?** They share a shape but have different lifecycles (monthly vs. per-scan), different inputs (marketing pages vs. findings), different prompts, and different UI. Reusing one table would muddy the "latest per competitor" query. Separate tables, same idioms.

## Extraction pipeline

### Inputs per competitor

- **Homepage** — `https://{competitor.homepage_domain}`. Required. If `homepage_domain` is NULL, skip the competitor with a logged warning.
- **Pricing page** — auto-probed at `/pricing` and `/plans` under the homepage domain. Included if the response is non-empty and non-bot-walled. Not required.
- **Product / features page** — auto-probed at `/product`, `/features`. Included if found. Not required.

Override mechanism: a new `positioning_pages: list[str]` JSON column on `Competitor` lets admins pin exact URLs. If set, *replaces* the auto-probe list — no merge semantics. Default is `[]`, meaning "auto-probe the three above". v1 surfaces this in the admin edit form as a textarea (one URL per line). Small, not the main work.

### Fetch

Pass each URL through `app.fetcher.fetch_content`. Apply the existing sanitizer and bot-wall detector. Concatenate the cleaned bodies with `\n\n--- {url} ---\n\n` separators. Cap at 40KB total (roughly 10K tokens) — truncate the tail, not the head, since marketing value is front-loaded on these pages.

Compute `source_hash = sha256(concatenated_text)`.

**Short-circuit:** if `source_hash` matches the competitor's latest snapshot, do not call the LLM. Return the existing snapshot as the "current view", log one info-level line, and exit. This makes monthly re-runs essentially free when nothing has changed.

### LLM calls

Two Haiku 4.5 calls, chained. Each uses a skill prompt loaded via `skills.load_active(...)`. Each is logged to `usage_events` separately so we can see cost per phase.

**Call 1 — `_extract_pillars(text, competitor)` → `pillars: list[dict]`**

- **System:** `load_active("positioning_extract")` (cached — same pattern as `llm_classify`).
- **User:** competitor name + category + concatenated fetched marketing text (with `--- {url} ---` separators). No prior-pillars context — extraction should be a fresh read of the pages, not anchored to last month's labels.
- **Response format:** single JSON `{"pillars": [...]}`. Schema enforced by the prompt; parsing is strict.
- **Failure:** malformed JSON → log raw response, raise. Caller skips this cycle, no snapshot written.

**Call 2 — `_write_narrative(pillars, prior_snapshot)` → `body_md: str`**

- **System:** `load_active("positioning_narrative")`.
- **User:** the pillars JSON from call 1, plus the prior snapshot's pillars JSON and date (if any). No marketing page text — the narrative reasons over the extracted pillars, not raw pages. This keeps the narrative prompt's context small and the call cheap.
- **Response format:** plain markdown body with the three required section headers (see the narrative skill below).
- **Failure:** any error → log, return `""`. Snapshot is still written with the pillars from call 1; the UI renders a muted "Narrative unavailable — retry refresh" beneath the pillar cards.

The pillars JSON schema is the contract between the two calls — if a future edit to `positioning_extract` changes the shape, `positioning_narrative` has to be updated to match. The schema lives in both prompts for clarity.

### Persist

Write one `PositioningSnapshot` row per successful extraction. Bump `competitor.last_activity_at`? No — that field means "saw something new about them in findings", keep it honest.

### Rate limit / concurrency

The monthly job iterates active competitors serially, one per minute. No parallelism in v1. Manual refresh from the UI is synchronous (one competitor, ~3 fetches + 1 LLM call, ~30s p95) — block the request, same as the existing "Regenerate review" button. If this becomes painful at >30 competitors, revisit with a background-task model; not the concern today.

## Skills

### `skill/positioning_extract.md`

```
---
name: positioning_extract
description: Extract positioning pillars as structured JSON from a competitor's own marketing pages. Call 1 of the positioning pipeline; the narrative pass runs on its output.
---

You are a positioning analyst doing structured extraction. Read one
competitor's marketing pages (homepage, pricing, product pages) and
identify the 3–6 **positioning pillars** they use to define themselves
right now.

A pillar is what the competitor's own pages try to convince a buyer
they are — a distinctive stance, not a feature list. Good pillars:
"AI-native workflow", "built for compliance-heavy industries",
"fastest vendor in APAC", "developer-first, not IT-first". Bad pillars:
"has a dashboard", "offers API access", "is a SaaS company".

## Inputs
- Competitor name, category
- Concatenated text from fetched marketing pages, with `--- {url} ---` separators

You will NOT see prior pillars. Extract fresh from the pages; the
narrative pass handles comparison to history.

## Output
Return a single JSON object. No prose, no markdown, no trailer.

{
  "pillars": [
    {
      "name": "short label, 2–4 words",
      "weight": 1..5,            // prominence; 5 = hero-level, 1 = mentioned
      "quote": "verbatim phrase from the page, ≤140 chars",
      "source_url": "the URL the quote came from"
    },
    ...
  ]
}

## Rules
- 3–6 pillars. If the pages are thin or boilerplate, return fewer
  (even 0) rather than padding.
- Pillars are stances, not features. If you can't tell why it matters
  strategically, it isn't a pillar.
- Prefer the competitor's own words for pillar names when they coin one.
- `quote` must be a verbatim substring of the input text. No paraphrasing.
- `source_url` must be one of the `--- {url} ---` markers from the input.
- Output JSON only. No backticks, no explanation.
```

### `skill/positioning_narrative.md`

```
---
name: positioning_narrative
description: Write a narrative synthesis of a competitor's positioning from the extracted pillars and the prior snapshot. Call 2 of the positioning pipeline.
---

You are a positioning analyst writing the narrative view of one
competitor's current positioning, and how it has shifted since the last
snapshot.

You do NOT see marketing page text. You see:
- Current pillars JSON (from the extraction pass)
- Prior pillars JSON + prior snapshot date (may be empty for first snapshots)

Your job is synthesis, not extraction. Reason over the pillars.

## Output
Plain markdown. Three sections, exact headers in this order:

## Current positioning
2–3 sentences. The competitor's stance as a whole — how the pillars
fit together into a posture. Not a list. Not a rephrasing of pillar
names. Something a strategist would say in a meeting.

## What changed since {prior_date}
- Bullets describing concrete shifts vs. the prior pillars.
- "New pillar: X." / "Dropped: Y." / "Reworded: Z was A, now B."
- Weight changes matter too: "'Enterprise-grade' went from weight 2 to
  weight 5 — they've moved it to the hero."
- If there is no prior snapshot: write exactly one line —
  "First snapshot — no comparison yet."
- If prior exists and nothing is materially different: write exactly one
  line — "No material change." Don't invent movement.

## Evidence
- One bullet per current pillar. Format: `**{name}** — "{quote}" ([source]({source_url}))`.
- Straight transcription from the pillars JSON. No commentary here.

## Style
- Confident, terse, analyst tone. Not marketing copy.
- Prefer one strong claim over five weak ones.
- No "synergy", "leverage", "ecosystem", "innovate".
- If fewer than 3 current pillars were extracted, still produce all three
  sections — just note the thin evidence in "Current positioning".
```

## UI — tab & panel

### Tab button

Added to `competitor_profile.html` in the existing `<nav class="tabs">`, between Momentum and Findings:

```html
<button role="tab" class="tab" data-tab="positioning"
        aria-selected="false" aria-controls="tab-positioning">Positioning</button>
```

The spec 02 JS already handles any `data-tab` value declared in the `TABS` array — add `'positioning'` to that array. No structural JS change.

### Panel

```html
<section class="tab-panel" id="tab-positioning" role="tabpanel" hidden>
  {% if positioning %}
    <div class="panel">
      <div class="panel-header">
        <h2>Current positioning</h2>
        <span class="muted">{{ positioning.created_at.strftime('%Y-%m-%d') }}</span>
        <form method="post" action="/competitors/{{ competitor.id }}/positioning/refresh">
          <button class="btn btn-secondary">Refresh positioning</button>
        </form>
      </div>
      {% include "_positioning_pillars.html" %}
      <div id="positioning-body" class="markdown"></div>   {# marked.js target #}
    </div>

    {% if positioning_history %}
      <div class="panel">
        <div class="panel-header"><h2>Prior snapshots</h2></div>
        <table class="history">
          {% for s in positioning_history %}
            <tr><td>{{ s.created_at.strftime('%Y-%m-%d') }}</td>
                <td>{{ s.pillars | length }} pillars</td>
                <td><button onclick="showPriorPositioning({{ s.id }})">view</button></td></tr>
          {% endfor %}
        </table>
      </div>
    {% endif %}
  {% else %}
    <div class="panel">
      <p class="muted">No positioning snapshot yet.</p>
      <form method="post" action="/competitors/{{ competitor.id }}/positioning/refresh">
        <button class="btn btn-primary">Capture first snapshot</button>
      </form>
      {% if not competitor.homepage_domain %}
        <p class="muted">Set <code>homepage_domain</code> on this competitor before running.</p>
      {% endif %}
    </div>
  {% endif %}
</section>
```

`marked.js` renders `positioning.body_md` into `#positioning-body` on load, same pattern as `renderReview()` for the Review tab — add a sibling `renderPositioning()` in the page's existing `<script>` block.

### Pillar cards (`_positioning_pillars.html`)

```html
<div class="pillar-grid">
  {% for p in positioning.pillars %}
    <div class="pillar-card">
      <div class="pillar-head">
        <span class="pillar-name">{{ p.name }}</span>
        <span class="pillar-weight" aria-label="Prominence {{ p.weight }} of 5">
          {% for i in range(5) %}<span class="dot {{ 'on' if i < p.weight else 'off' }}"></span>{% endfor %}
        </span>
      </div>
      <blockquote class="pillar-quote">{{ p.quote }}</blockquote>
      <a class="pillar-source" href="{{ p.source_url }}" target="_blank" rel="noopener">source</a>
    </div>
  {% endfor %}
</div>
```

## CSS additions

Add alongside `.signal-card` / `.panel` rules in `app/static/style.css`:

```css
.pillar-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
  margin: 16px 0;
}
.pillar-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--bg-subtle);
}
.pillar-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.pillar-name { font-weight: 600; font-size: 14px; }
.pillar-weight .dot {
  display: inline-block; width: 6px; height: 6px; border-radius: 50%;
  margin-left: 2px; background: var(--border);
}
.pillar-weight .dot.on { background: var(--text); }
.pillar-quote {
  margin: 8px 0 6px; padding: 0; border-left: 2px solid var(--border);
  padding-left: 8px; color: var(--text-dim); font-size: 13px; line-height: 1.4;
}
.pillar-source { font-size: 11px; color: var(--text-dim); }
```

Bump the CSS cache-buster to `?v=positioning-1` on `base.html`.

## Scheduler

Add to `app/scheduler.py`:

- New job `positioning_refresh_monthly`, runs on the 1st of each month at 02:00 UTC (well clear of the daily scan window). Configurable via env: `POSITIONING_REFRESH_CRON`, default `0 2 1 * *`.
- For each active competitor with a non-null `homepage_domain`: call `extract_positioning`, catch and log exceptions per competitor (one failure doesn't abort the batch), sleep 60s between competitors.
- No `Run` row is created — this job is logged via the existing scheduler logging, not the scan-run UI. `Run` is for scans.

## Manual refresh endpoint

`POST /competitors/{id}/positioning/refresh`:

- Auth: same guard as `/competitors/{id}/regenerate_review`.
- Calls `extract_positioning(competitor, db)` synchronously. Expect ~30s.
- On success: 303 redirect to `/competitors/{id}#positioning`.
- On failure: 303 redirect to `/competitors/{id}#positioning` with a flash-style error (one new field on the rendering context, rendered as a `<p class="error">` in the panel). Don't blow up the page.

## Competitor admin edit form

Add one new textarea: `positioning_pages` (optional, one URL per line). Saved to `competitor.positioning_pages`. Empty = auto-probe. No other UI changes needed.

## Testing

- **Model/migration.** Migration runs forward/back clean on a dev DB. Model imports without error.
- **Extraction on a seeded competitor.** Set `homepage_domain=example.com` on a seeded competitor, run `extract_positioning` in a REPL. Assert: one `PositioningSnapshot` row written, `pillars` is a non-empty list, `body_md` contains the three required headers, `source_urls` is a subset of the attempted URLs.
- **Short-circuit.** Re-run immediately; assert no new row was written (hash match) and the function returned the existing snapshot.
- **Malformed extraction JSON.** Mock call 1 to return non-JSON; assert `extract_positioning` raises, no row is written, scheduler loop continues to the next competitor.
- **Narrative call failure.** Mock call 1 to succeed and call 2 to raise; assert one row is written with `pillars` populated and `body_md == ""`. Tab renders pillar cards plus a muted "Narrative unavailable — retry refresh" line.
- **Skill seeding.** Fresh DB: after `sync_files_to_db()` runs at boot, assert one active `Skill` row each for `positioning_extract` and `positioning_narrative`, both version 1. `/settings/skills` lists them.
- **Bot-wall page.** Mock the fetcher to return a bot-wall for the homepage; assert the extraction either skips (if zero pages come back non-empty) or proceeds with the remaining pages.
- **UI default tab.** Existing `/competitors/{id}` still defaults to Review. `#positioning` in the hash activates the new tab. `#bogus` falls back to Review.
- **UI empty state.** A competitor with no snapshot renders the "Capture first snapshot" button. A competitor with `homepage_domain=NULL` renders the additional warning.
- **UI refresh button.** Clicking it on a competitor with a valid `homepage_domain` writes a new snapshot (or short-circuits if unchanged) and lands back on `#positioning`.
- **Prior snapshot history.** Insert two snapshots for the same competitor; second tab render shows the later one as current and the earlier one in the history table.
- **Visual.** Pillar cards lay out in a responsive grid (at least 2 cols at 1280px, 1 col at 375px). Weight dots render correctly 1–5.

## Acceptance criteria

1. New `positioning_snapshots` table migrates forward and back cleanly. `PositioningSnapshot` imports from `app.models`.
2. `skill/positioning_extract.md` and `skill/positioning_narrative.md` exist, are listed in `KNOWN_SKILLS`, and are seeded into the `skills` table on first boot. Both are editable + versioned via `/settings/skills`.
3. `app/signals/positioning.py::extract_positioning(competitor, db)` makes two Haiku calls (extract then narrative), both loading prompts via `skills.load_active(...)`, both logging to `usage_events`. Writes exactly one snapshot per successful extraction; short-circuits via `source_hash` when pages haven't changed. Fetch errors or malformed extraction JSON → no snapshot. Narrative-call failure → snapshot written with empty `body_md` and the pillars intact.
4. Scheduler runs `positioning_refresh_monthly` per cron and iterates active competitors without letting one failure abort the batch.
5. Competitor profile page has a **Positioning** tab (fourth in the bar) that renders pillar cards from the latest snapshot, the "what changed" body via marked.js, and a prior-snapshots history table.
6. `POST /competitors/{id}/positioning/refresh` runs a single extraction synchronously and redirects to `#positioning`.
7. All existing tabs, JS, regenerate-review, and scan behaviour continue to work unchanged.
8. CSS cache-buster bumped so pillar styles load without a hard refresh.

## What this unblocks

- **Landscape view (next spec).** A `/positioning` page that loads the latest `PositioningSnapshot` per active competitor and renders a matrix of competitors × pillar-name-clusters. The data source is already in place.
- **Messaging-shift findings.** When `extract_positioning` writes a new snapshot whose pillar set differs from the prior one, auto-file a `Finding` with `signal_type="messaging_shift"`, `payload={prior_pillars, new_pillars, diff}`. One-line extension to `extract_positioning`; deliberately not in v1 so we can see a few snapshots first and decide what a useful diff threshold is.
- **Deep-links from the market digest.** Digest can now link `/competitors/42#positioning` when it references a competitor's narrative shift.
- **Per-pillar prevalence queries.** "How many competitors claim 'AI-native' as a pillar?" is a simple SQL query against `json_each(pillars)`. No extra schema needed.
