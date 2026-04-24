# Spec 05 — Market Deep Synthesis (Gemini, cross-competitor)

**Status:** Draft
**Owner:** Simon
**Depends on:** 04 (per-competitor Deep Research — reuses the `gemini_research.py` adapter, `GEMINI_API_KEY`, and the "create → poll → persist" job shape).
**Unblocks:** a single on-demand read of the whole market; weekly investor-grade synthesis without human stitching; a future "research landscape" page that cross-references syntheses over time.

## Purpose

The existing market digest at `/market` is the everyday read: one Claude call over the last scan's findings + per-competitor reviews, tuned for *volume* and *recency*. It's cheap, fast, emailed daily, and based entirely on what our own scanners surfaced.

What it *doesn't* do is go deep across competitors. It can't cross-reference an Indeed pricing move against a LinkedIn hiring surge, an Ashby product launch, and a Sidekicker segment shift in the same read, grounded in primary sources beyond what our scan window pulled. It doesn't pull analyst notes, earnings calls, or regulatory filings. It runs in seconds per report — deep narrative synthesis isn't on the table at that budget.

**Market Deep Synthesis** fills that gap. Take the same inputs Deep Research gets for one competitor, but stitch them across *all* active competitors — 30 days of findings, latest per-competitor review, latest per-competitor Deep Research report — and hand the whole package to Gemini Deep Research. 5–20 minutes later we have a cited cross-market read: where the market is moving, which competitors are accelerating, where the consensus is forming, where Seek needs to respond.

One row per run, stored append-only like every other synthesized artifact. Weekly cron by default; manual "Run" button for unscheduled deep dives.

## Non-goals

- **Replacing the existing market digest.** The daily Claude digest stays. It's the fast/cheap read on what just landed; deep synthesis is the weekly/on-demand read on what it all *means*. Same coexistence pattern as Review + Research on the competitor profile.
- **Streaming tokens.** Gemini Deep Research is background-only; we poll for status, not tokens. Match the Spec 04 job shape.
- **Per-segment syntheses.** v1 is one read across all active competitors. Filtering by category ("just ATS competitors", "just labour-hire") is a future knob, not v1.
- **Strategy-doc re-upload.** The brief references the strategy context that's already present in `ContextBrief` rows (company + customer). No new upload surface.
- **Private-file ingestion / custom MCP servers.** Same Web-only-Google-grounding scope as Spec 04.
- **Editing the synthesis prompt from the UI.** Lives as a skill file at `skill/market_synthesis_brief.md`, editable at `/settings/skills` like every other skill.
- **Feeding synthesis reports back into the daily digest prompt.** These are their own artifact. Revisit after a few weeks of reports exist and we can see what's worth folding back.
- **Emailing the synthesis.** Not in v1. The daily digest already goes to the team; syntheses live on the Market page until we've validated the shape. Email is a trivial follow-up once the shape is right.

## Design principles

1. **Append-only, one row per run, latest = current.** Mirror `DeepResearchReport` exactly — differs only in that it's not keyed by `competitor_id`. No updates, no deletes in the happy path.
2. **Weekly cron + manual trigger.** Scheduler drops one run every Monday at 03:00 (local TZ). A "Run synthesis" button on `/market` lets the team kick off an unscheduled one when something big breaks. Cron defaults to `max` agent; manual defaults to `preview` — weekly runs get depth, ad-hoc runs get speed.
3. **One skill, one file.** `skill/market_synthesis_brief.md` with placeholders `{{our_company}}`, `{{our_industry}}`, `{{window_days}}`, `{{competitor_context}}`, `{{findings_digest}}`, `{{deep_research_digest}}`, `{{company_brief}}`, `{{customer_brief}}`.
4. **HTMX status card, no WebSocket.** Same poll-every-10s-back-off-to-30s pattern as the Research tab. Stops on terminal states.
5. **Reuse the Gemini adapter as-is.** `gemini_research.start_research(brief, agent)` + `poll_research(interaction_id)` already return exactly the shape we need. No adapter changes — this spec is consumer-side only.
6. **Reuse the `Run` / `RunEvent` infrastructure.** New `kind="market_synthesis"`. Shows on `/runs`, emits `material` event on success. Same resume-on-boot sweep as Deep Research.
7. **Cap the brief size defensibly.** 30-day findings can easily hit hundreds of rows and the DR reports are big. Use `Finding.summary` not `content`; cap per-competitor to top-20 by recency; truncate DR reports to their first ~2000 chars plus their watchlist section. Log the composed brief size as a RunEvent so we can see when we're brushing against limits.
8. **Fail soft.** `GEMINI_API_KEY` missing → the "Run synthesis" button is replaced by the "Add a key" nudge (same pattern as Spec 04). Adapter errors surface in the status card and the `Run` row.
9. **Two panels on one page.** `/market` gains a **Deep Synthesis** panel above the existing **Market digests** panel. Synthesis latest renders inline, history collapses below. The daily-digest table stays put, unchanged. No new top-level nav.

## Where it lives

- **Model + migration**
  - `app/models.py` — new `MarketSynthesisReport` class (shape below).
  - `alembic/versions/<new>_market_synthesis_reports.py` — create table with indexes.
- **Integration layer**
  - No changes to `app/adapters/gemini_research.py`. Reused as-is.
  - No changes to `app/env_keys.py` — `GEMINI_API_KEY` already registered for Spec 04.
- **Prompt**
  - `skill/market_synthesis_brief.md` — the new template.
  - `app/skills.py::KNOWN_SKILLS` — register `market_synthesis_brief`.
- **Brief composer**
  - `app/market_synthesis.py` — new module. Pure function `compose_brief(db: Session, *, window_days: int = 30) -> tuple[str, dict]` returns the filled skill template and a metadata dict (`{findings_count, competitors_covered, dr_reports_used, brief_chars}`) that the job wrapper writes to a RunEvent for observability.
- **Job**
  - `app/jobs.py` — new `run_market_synthesis_job(triggered_by: str = "manual", agent: str = "preview", window_days: int = 30)`. Creates a `Run` with `kind="market_synthesis"`, composes the brief, starts a Gemini interaction, writes a pending `MarketSynthesisReport` row, polls until terminal. Same failure/timeout/resume semantics as `run_deep_research_job`.
- **Scheduler**
  - `app/scheduler.py` — new weekly cron job `id="market_synthesis_weekly"`, default `CronTrigger(day_of_week='mon', hour=3, minute=0)`, overridable via `MARKET_SYNTHESIS_CRON="min hour dow"` env var. Passes `agent="max", triggered_by="scheduled"`.
  - Resume-on-boot sweep extends the Spec 04 pattern to also cover `MarketSynthesisReport` rows in `queued`/`running`.
- **Routes**
  - `app/ui.py::market_index` — extend to also load `latest_synthesis: MarketSynthesisReport | None` + `synthesis_history: list[MarketSynthesisReport]` + `running_synthesis: Run | None`. Template reads both the existing `reports` and the new synthesis fields.
  - `app/ui.py` — new `GET /market/synthesis/{id}` for the detail view.
  - `app/ui.py` — new `GET /market/synthesis/{id}/status` HTMX partial endpoint for the poller.
  - `app/routes/runs.py` — new `POST /api/runs/market-synthesis` (mirrors the existing `/api/runs/market-digest`). Auth-gated (admin or analyst — match "Regenerate digest"). Accepts optional `agent` + `window_days` query params.
- **Templates**
  - `app/templates/market_index.html` — extend to render the new **Deep Synthesis** panel above the existing digest table. Status card (if a run is active) or latest synthesis summary card (title + excerpt + "View full synthesis") or empty state with Run button.
  - `app/templates/market_synthesis_detail.html` — new template. Header (started_at, agent, duration, cost), body via marked.js, sources grid, prior-runs `<details>` collapsible.
  - `app/templates/_synthesis_status.html` — new partial rendering the status card states (idle / queued / running / ready / failed / cancelled). Single partial returned by both the initial `/market` render and the polling endpoint.
- **CSS**
  - `app/static/style.css` — reuse `.research-status`, `.citation-chip` classes from Spec 04 (rename if they're still `.research-*` — they should be general-purpose from day one, not research-specific). Bump `?v=` cache-buster.
- **JS**
  - None new. Reuse the existing HTMX poller pattern.

## Data model

```python
class MarketSynthesisReport(Base):
    """One Gemini Deep Research run across the entire competitor set.
    Append-only: latest is the 'current' view, older rows are history
    surfaced as a collapsible list on the Market page.

    Created in 'queued' state by the weekly cron or the manual Run
    button. The job flips to 'running' once Gemini confirms the
    interaction is active, 'ready' when the final report is written,
    or 'failed' with the error message captured in `error`.

    Unlike DeepResearchReport, there is no competitor_id — this is
    one report covering all active competitors simultaneously.
    """
    __tablename__ = "market_synthesis_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)

    # Gemini-side identifier, so we can reconnect to an in-flight run
    # after a server restart (poll on boot, resume the status).
    interaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # "preview" | "max". Frozen at creation time.
    agent: Mapped[str] = mapped_column(String(32), default="preview")

    # "queued" | "running" | "ready" | "failed" | "cancelled"
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)

    # "manual" | "scheduled". Tells us which path created this row —
    # useful when debugging cron vs. button behaviour.
    triggered_by: Mapped[str] = mapped_column(String(16), default="manual")

    # Lookback window used when composing the brief (days). Stored so
    # syntheses remain interpretable even if we change the default.
    window_days: Mapped[int] = mapped_column(Integer, default=30)

    # Filled-in skill template sent to Gemini. Persisted so the exact
    # ask is auditable and rerunnable.
    brief: Mapped[str] = mapped_column(Text, default="")

    # Final report markdown. Empty until status flips to 'ready'.
    body_md: Mapped[str] = mapped_column(Text, default="")

    # Structured citations from Gemini (same shape as DeepResearchReport.sources):
    #   [{"title": str, "url": str, "published_at": str | None,
    #     "snippet": str | None}]
    sources: Mapped[list] = mapped_column(JSON, default=list)

    # Composition metadata written by the brief composer. Shape:
    #   {"findings_count": int, "competitors_covered": int,
    #    "dr_reports_used": int, "brief_chars": int}
    # Surfaced in the report header so we can see what the synthesis
    # was built from, and catch silent input drops early.
    inputs_meta: Mapped[dict] = mapped_column(JSON, default=dict)

    # Human-readable error when status='failed'. None otherwise.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
```

**Indexes:**
- `started_at DESC` — primary access pattern (latest + history).
- `interaction_id` — for the resume-on-boot sweep.
- `status` — for the "is a run in flight?" check.

## Brief composition

`app/market_synthesis.py::compose_brief(db, *, window_days=30)` builds the payload in four passes:

1. **Company + customer context** — pull the latest `ContextBrief` rows for `scope="company"` and `scope="customer"`. Inserted into `{{company_brief}}` and `{{customer_brief}}`. If either is missing, the placeholder renders as `(not available)` and a RunEvent warns — we still proceed.

2. **Per-competitor context block** — for each active `Competitor`, compose a short stanza:
   ```
   ### {{name}} ({{category}})
   Threat angle: {{threat_angle if set}}
   Latest review ({{review.created_at}}): {{review.body_md truncated to 2000 chars}}
   Latest deep-research ({{dr.started_at if present}}): {{dr.body_md truncated to 2000 chars + sources count}}
   ```
   Inserted into `{{competitor_context}}`.

3. **Findings digest** — all `Finding` rows from the last `window_days` days, grouped by competitor, newest first. Per competitor, keep at most 20 rows. Each row renders as one bullet: `{{published_at or created_at}} · {{signal_type}} · {{digest_threat_level}} — {{summary or title}}`. Inserted into `{{findings_digest}}`. Never raw URLs inline (keeps the brief skimmable for Gemini).

4. **Deep-research digest** — a separate pass listing the titles + watchlist sections extracted from each competitor's latest `DeepResearchReport`, so the synthesis can reference what went into the per-competitor dossiers. Inserted into `{{deep_research_digest}}`. Skipped competitors with no DR report.

The composer returns the filled template plus an `inputs_meta` dict written into the `MarketSynthesisReport.inputs_meta` column. That dict — findings_count, competitors_covered, dr_reports_used, brief_chars — is what we'll look at first when a synthesis reads thin.

**Size budget.** Rough upper bound at 20 active competitors: ~400 findings at ~400 chars each = 160KB + 20 × 2KB reviews + 20 × 2KB DR excerpts = ~240KB. Gemini accepts that. If the composed brief exceeds 500KB, log a `level="warn"` RunEvent and truncate the oldest findings first — a synthesis on truncated inputs is still better than a failed one.

## Execution flow

1. **Cron path** — Monday 03:00 local time, APScheduler calls `run_market_synthesis_job(triggered_by="scheduled", agent="max", window_days=30)`.
2. **Manual path** — user clicks **Run synthesis** on `/market`. POST `/api/runs/market-synthesis?agent=preview&window_days=30`. Server enqueues `run_market_synthesis_job(triggered_by="manual", agent=..., window_days=...)` and returns 202 JSON (matches `/api/runs/market-digest` contract).
3. **Job wrapper:**
   - Creates a `Run(kind="market_synthesis", triggered_by=..., status="running")`.
   - Composes the brief via `market_synthesis.compose_brief()`. Logs `level="info"` RunEvent with `inputs_meta`.
   - Creates a `MarketSynthesisReport(status="queued", brief=..., run_id=..., agent=..., window_days=..., triggered_by=..., inputs_meta=...)`.
   - Calls `gemini_research.start_research(brief, agent=agent)`. Updates the row: `interaction_id`, `status="running"`. Logs RunEvent "Synthesis started (agent=..., interaction=...)".
   - Polls every ~20s via `gemini_research.poll_research(interaction_id)`. Hard timeout 30 min (configurable via `MARKET_SYNTHESIS_TIMEOUT_S`).
   - On terminal success: persist `body_md`, `sources`, `finished_at`, `cost_usd`, `model`. Flip `status="ready"`. Close the `Run`. Log `level="material"` RunEvent with `meta={synthesis_id, title="Market synthesis · YYYY-MM-DD"}`.
   - On terminal failure/timeout: flip `status="failed"`, store `error`, close the `Run` with `status="error"`.
4. **HTMX poll** sees `status="ready"` and swaps the status card for the report summary card. Full body rendered at `/market/synthesis/{id}`.

### Resume-on-boot

App lifespan startup extends the Spec 04 sweep: after re-polling `DeepResearchReport` rows in `queued`/`running`, do the same for `MarketSynthesisReport`. Same three outcomes (done → persist, running → re-enqueue poller, lost → mark failed). Shared helper in `app/jobs.py::_resume_gemini_runs()` so both specs use one code path.

## Prompt (`skill/market_synthesis_brief.md`)

Sketch, not final:

```markdown
You are producing a weekly market synthesis for {{our_company}} in {{our_industry}}.
Your audience is the {{our_company}} strategy team — smart, time-poor, want signal not noise.

Use the inputs below as grounding for what our own competitor-watch system has
observed in the last {{window_days}} days. Go beyond them: cross-reference with
primary sources (earnings calls, SEC filings, product changelogs, verified
executive statements), recent analyst coverage, and regulatory filings where
relevant. Ground every claim in cited sources.

**Our company context:**
{{company_brief}}

**Our customer context:**
{{customer_brief}}

**Competitor roster + recent per-competitor read:**
{{competitor_context}}

**Cross-competitor findings digest (last {{window_days}} days):**
{{findings_digest}}

**Per-competitor deep-research excerpts (for reference):**
{{deep_research_digest}}

Produce a synthesis with these sections:

1. **TL;DR** — one paragraph. Lead with the single most important market-level
   movement this period. If nothing material shifted, say so plainly.

2. **Market movements** — 3–6 cross-competitor narratives. Each is a theme
   (e.g. "AI matching is consolidating around three approaches", "ATS
   platforms are quietly building job distribution"), not a per-competitor
   dump. Cite the competitors and sources that anchor each narrative.

3. **Acceleration vs. deceleration** — which competitors have picked up pace
   this period, which have gone quiet, and what that likely means.

4. **Where the market is converging** — strategies, features, or pricing
   moves showing up across multiple competitors simultaneously.

5. **Where the market is diverging** — segments or strategies where
   competitors are actively betting in different directions.

6. **Implications for {{our_company}}** — specific, ranked. For each item,
   name the {{our_company}} initiative or product it touches, and whether
   it's threat, opportunity, or neutral-context.

7. **Watchlist** — 3–5 specific signals the team should watch in the next
   6 weeks that would materially change this read.

Prefer primary sources. Label speculative claims as such. Don't pad sections —
a short synthesis with real signal beats a long one with filler.
```

Stored as a regular skill so we can iterate it at `/settings/skills` and version it alongside the rest.

## UI

### `/market` layout

```
Market

  [ Run synthesis ]  ← manual trigger, primary action
  [ Regenerate digest ]  ← existing secondary action, unchanged

  ┌─ Deep Synthesis ─────────────────────────────────┐
  │ <latest synthesis card or status card>          │
  │ ▸ Prior syntheses (N)                            │
  └──────────────────────────────────────────────────┘

  ┌─ Market digests (daily) ─────────────────────────┐
  │ <existing table of Report rows, unchanged>       │
  └──────────────────────────────────────────────────┘
```

### Synthesis panel states

Renders from `_synthesis_status.html`:

- **No row yet (empty state)**
  > No market synthesis yet.
  > Weekly synthesis runs Monday 03:00, or kick one off now.
  > Takes 5–20 minutes, costs ~$3–10 per run.
  > [ Run synthesis ]

- **queued**
  > Queued · just now
  > Composing brief — {{competitors_covered}} competitors, {{findings_count}} findings...

- **running**
  > Running · 4m 18s elapsed
  > Gemini is synthesizing — this usually takes 5–20 minutes.
  > [ Cancel ]

- **ready**
  > **{{title}}** · {{started_at}} · {{duration}} · {{agent}} · {{cost_usd}}
  > {{first 300 chars of body_md as excerpt}}
  > [ Read full synthesis → /market/synthesis/{id} ]

- **failed**
  > Failed · {{error}}
  > [ Try again ]

- **cancelled**
  > Cancelled · {{when}}
  > [ Run again ]

### Detail page (`/market/synthesis/{id}`)

- Header row: `Market Synthesis · {{started_at}} · {{agent}} · {{duration}} · {{cost_usd}}`.
- Inputs line: `Built from {{findings_count}} findings across {{competitors_covered}} competitors; {{dr_reports_used}} deep-research reports referenced.` (Gives the reader instant confidence/concern about coverage.)
- Body: `marked(report.body_md)`, same renderer as the digest detail page.
- Sources: grid of `.citation-chip` elements.
- Below the body: collapsible `<details>` with prior syntheses (one-line summary → expands to the full `market_synthesis_detail.html` body via HTMX lazy-load, matching the Review tab history pattern).

### Run button placement

- On `/market`, the "Run synthesis" button is a primary action in the page header (next to the existing "Regenerate digest" ghost button).
- Confirm-on-click: `confirm("Run a new market synthesis? Takes 5–20 minutes and costs ~$3–10.")`. Same as Spec 04.

## Cost & rate-limit discipline

- **Per-install cooldown.** If the latest synthesis for this install is `ready` and `started_at < 6h ago`, the Run button becomes a "Fresh synthesis from 2h ago — Run anyway?" confirm. Prevents accidental double-runs. Cooldown doesn't apply to the cron path (that's expected to run on schedule).
- **Global concurrency cap of 1.** Only one synthesis at a time. A second click while one is running returns a friendly "Synthesis already running — wait for it to finish" message. Cron respects the same lock.
- **Hard timeout.** 30 min (covers the 5–20 min window + buffer). Configurable via `MARKET_SYNTHESIS_TIMEOUT_S`.
- **Cron guard.** If a manual run is in flight when the weekly cron fires, the cron is skipped (no queue, single-shot semantics — we don't want a sleepy Monday morning to surface *two* overlapping syntheses).

## Auth

- `POST /api/runs/market-synthesis` requires admin or analyst role. Same gate as `/api/runs/market-digest` and the positioning refresh endpoint.
- `GET /market`, `GET /market/synthesis/{id}`, and the status poll endpoint are readable by anyone with `/market` access today.

## Testing

- Open `/market` with no synthesis rows. Shows the empty-state card with Run button.
- With `GEMINI_API_KEY` unset, Run button is replaced by the "Add a key" nudge pointing at `/settings/keys`.
- Click Run. UI shows "queued" within 1s, then "running" once Gemini confirms. HTMX poller active.
- Leave `/market` and navigate back. Status card picks up where it was.
- Stub the adapter: force a `ready` response. Status card swaps to the summary card with excerpt + link. Click through to the detail page — body + sources + inputs line render.
- Stub a `failed` response. Error surfaced in card; "Try again" re-enqueues.
- Restart the server mid-run. Boot sweep re-polls. Status card continues to update.
- Cooldown: run one, then within the cooldown window click Run again. Confirm prompt appears.
- Concurrency: start a synthesis, try to start a second. Second returns the friendly error.
- Cron: override `MARKET_SYNTHESIS_CRON` to fire ~1 minute from now, confirm it runs with `agent="max"` and `triggered_by="scheduled"`.
- `/runs` shows the `market_synthesis` runs with kind + agent + duration. Live log shows the `material` event on success with a clickable link to `/market/synthesis/{id}`.
- `inputs_meta` surfaces correctly in the detail-page "inputs line" and is present in the `MarketSynthesisReport` row.
- The existing daily digest panel on `/market` is visually unchanged and still functional.

## Acceptance criteria

1. `/market` renders a Deep Synthesis panel above the existing digest table. Empty state, queued, running, ready, failed, cancelled all render without errors.
2. Clicking Run enqueues a `run_market_synthesis_job`, writes a `MarketSynthesisReport` row in `queued`, and the page updates to show the running state within one HTMX poll tick.
3. The status card polls at 10s (backing off to 30s after 5 min), stops on terminal states, and updates without a full page reload.
4. Completed syntheses render at `/market/synthesis/{id}` with body_md via marked.js, sources as clickable chips, and an inputs-line showing composition meta.
5. Missing `GEMINI_API_KEY` shows a key-missing nudge; Run button hidden. Daily digest panel remains fully functional (doesn't need Gemini).
6. Weekly cron runs Monday 03:00 local TZ with `agent="max"`, overridable via `MARKET_SYNTHESIS_CRON`.
7. Cron is skipped if a manual run is already in flight (no queueing, no overlap).
8. Failures/timeouts/mid-run restarts are all recoverable via the shared `_resume_gemini_runs` boot sweep.
9. Cooldown + concurrency cap enforced with clear UI messaging.
10. Syntheses appear on `/runs` with `kind="market_synthesis"`, and in the live log (`level="material"` on success).
11. CSS cache-buster bumped; Research tab's `.research-status` / `.citation-chip` classes either remain research-specific and get a synthesis-specific copy, OR are generalized (rename once, update both call sites).
12. Existing `/market/{id}` digest detail route, daily digest cron, and the "Regenerate digest" button all behave identically to before.

## What this unblocks

- **Emailing the synthesis.** Once the shape is validated, add a mailer call at the end of `run_market_synthesis_job` that sends the synthesis to the same team distribution the daily digest uses. Trivial incremental change.
- **Category-scoped syntheses.** Add an optional `category` argument to the job and compose_brief — one synthesis just for ATS competitors, or just labour-hire. Same table, extra column, same UI pattern.
- **Synthesis-of-syntheses.** Quarterly/annual roll-up that reads the last N syntheses as input. Same table, different prompt, different cadence.
- **Citation analytics across all Gemini runs.** Deep Research + Market Synthesis now share a sources shape — we can answer "what domains does Gemini lean on for our market?" over the combined corpus.
- **Feedback loop into the daily digest.** If the synthesis consistently surfaces narratives the daily digest missed, that's evidence to adjust the digest prompt or the scan keyword set. Human read in both directions initially.
- **"Research landscape" page.** A future `/research` surface listing syntheses + all competitor deep-research runs in one cross-market timeline. The tables are already the right shape for it.
