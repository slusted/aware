# Spec 04 — Deep Research tab (Gemini)

**Status:** Draft
**Owner:** Simon
**Depends on:** 02 (tabbed competitor profile — this spec adds a fifth tab using the same `[hidden]`/hash-driven system).
**Unblocks:** investor-grade per-competitor dossiers on demand; future "weekly Max report" cron without further UI work; a future "Research" surface that spans multiple competitors.

## Purpose

The Review tab synthesizes last-~6-weeks of findings into a short strategy read. It's tuned for *volume* and *recency*, not *depth*. When Simon wants to go deep on one competitor — for a board paper, a positioning debate, or a pre-meeting brief — the review is too shallow: it only sees the ~60 URLs Tavily returned in the scan window, it doesn't cross-reference analyst notes, product docs, earnings calls, or regulatory filings, and it never spends more than a few seconds of LLM time per competitor.

Google's **Gemini Deep Research** (Interactions API, launched Dec 2025; Max tier launched Apr 2026) is purpose-built for this gap: give it a research brief, it autonomously plans, runs up to 160 web searches, reads the results, and returns a fully cited report in 5–20 minutes. Wiring it in as a new **Research** tab on the competitor profile gives us on-demand investor-grade dossiers without building a second scan pipeline.

One tab, one button, one report per run, stored append-only like every other synthesized artifact on this page.

## Non-goals

- **Replacing the Review tab.** Review is the everyday read — fast, cheap, and based on our own curated findings. Deep Research is the occasional deep dive. They coexist; neither subsumes the other.
- **Auto-scheduling.** v1 is user-triggered only. Runs are minutes long and cost real money (order of dollars per report), so a nightly cron across all competitors is not in v1. Nightly / weekly Max runs are [What this unblocks](#what-this-unblocks), not scope.
- **Streaming tokens to the UI.** The Gemini Interactions API is background-only for Deep Research — a task runs 5–20 minutes and returns the complete report at the end. We poll for status, not tokens.
- **Multi-competitor synthesis.** "Compare Indeed vs. LinkedIn across these six pillars" is a different surface (a future landscape view). This spec is one competitor at a time.
- **Private-file ingestion.** Deep Research Max supports searching your private files; we don't have anywhere to upload them today. Web-only for v1.
- **Custom MCP servers.** Deep Research supports MCP, but wiring our own scraper/Tavily as MCP is a separate spike. v1 uses Gemini's built-in Google Search grounding only.
- **Editing the research prompt from the UI.** The prompt is a skill file under `skill/`, versioned like every other skill, editable at `/settings/skills`. Not a per-run free-text box in v1.
- **Feeding Deep Research reports back into the market digest or the review synthesizer.** These reports are their own artifact, rendered on their own tab. No crossover in v1 — revisit once we have a few weeks of reports in the table and can see what's worth folding back.

## Design principles

1. **Append-only, like `CompetitorReport` and `PositioningSnapshot`.** One row per run. Latest = current view; older rows = collapsible history. No updates, no deletes in the happy path. Steal the shape from `CompetitorReport`.
2. **User-triggered only.** A single "Run Deep Research" button on the tab enqueues a run. No auto-refresh on interval, no run-on-every-scan. The user decides when a deep dive is worth it.
3. **Preview agent by default; Max as an explicit opt-in.** `deep-research-preview` is the right tool for interactive surfaces (faster, cheaper, ~5–10 min). `deep-research-max` is reserved for future scheduled/background runs where exhaustiveness matters more than latency (future scope). v1 UI ships with Preview only — one button, no dropdown, no confusion.
4. **Gemini's `background=True` is the whole execution model.** We create an Interaction, immediately get an ID back, and poll. Our job wrapper is almost entirely "create → poll → persist" — no stdout tee, no threadpool, no mid-flight cancellation of the Gemini side. Our side of the run can still be cancelled (marks the row as cancelled and stops polling); the Gemini task keeps running and is left to complete on their side rather than paying to abort.
5. **One skill, one file.** `skill/deep_research_brief.md` holds the prompt template (competitor name / category / company context / focus areas → research brief). Registered in `app/skills.py::KNOWN_SKILLS` like every other skill. Editing it changes what Deep Research is asked to do, without a code deploy.
6. **HTMX for the status card, no WebSocket.** The tab renders a status card that polls a partial endpoint every 10 seconds while a run is active. Backs off to 30s after 5 minutes. Stops polling when status is terminal. Matches the stack discipline from the other surfaces — no new JS machinery.
7. **Store the full report + structured sources.** The Interactions API returns markdown + a sources/citations list. Persist both: the body_md for rendering (via marked.js, as everywhere else), and the sources as a JSON payload so we can render them as clickable citation chips and later run analytics on what Deep Research is pulling from.
8. **Fail soft.** If `GEMINI_API_KEY` is missing, the tab renders an "Add a key" nudge pointing at `/settings/keys` (same pattern as the existing Tavily/Anthropic key-missing states). If Gemini returns an error mid-run, the row is marked `failed` with the error message visible in the tab. Partial results (if Gemini ever returns them) are stored if present.
9. **One Run per report.** Reuses the existing `Run` / `RunEvent` infrastructure — `kind="deep_research"`, `triggered_by="manual"`. Appears on `/runs` and in the live log like any other pipeline. Cost visibility + audit trail for free.

## Where it lives

- **Model + migration**
  - `app/models.py` — new `DeepResearchReport` class (shape below).
  - `alembic/versions/<new>_deep_research_reports.py` — create the table with the indexes below.
- **Integration layer**
  - `app/adapters/gemini_research.py` — new thin adapter. `start_research(brief: str, agent: str = "preview") -> str` returns an interaction_id. `poll_research(interaction_id: str) -> dict` returns `{status, body_md, sources, error}`. Isolates the Gemini SDK call from the job wrapper so we can stub it in tests.
  - `app/env_keys.py::MANAGED_KEYS` — add `GEMINI_API_KEY` with description "Gemini Deep Research (per-competitor deep dives)". Key refresh handler mirrors the pattern for `ANTHROPIC_API_KEY` if the adapter ends up caching a client.
- **Prompt**
  - `skill/deep_research_brief.md` — Markdown prompt template with `{{competitor_name}}`, `{{category}}`, `{{our_company}}`, `{{our_industry}}`, `{{threat_angle}}`, and `{{watch_topics}}` placeholders.
  - `app/skills.py::KNOWN_SKILLS` — new entry registering the skill.
- **Job**
  - `app/jobs.py` — new `run_deep_research_job(competitor_id: int, agent: str = "preview", triggered_by: str = "manual")`. Creates a `Run` with `kind="deep_research"`, starts a Gemini interaction, writes a pending `DeepResearchReport` row, then polls until terminal. Writes `RunEvent`s for `started`, `step` progress (if the API exposes intermediate step messages), `completed`, or `failed`.
- **Scheduler**
  - No change for v1. The job is manual-only.
- **Routes**
  - `app/routes/competitors.py` — the existing competitor profile loader gains `latest_research: DeepResearchReport | None` + `research_history: list[DeepResearchReport]` + `running_research: Run | None`, the same way it already loads `CompetitorReport` + history + active scan state.
  - `app/routes/competitors.py` — new `POST /competitors/{id}/research/run` endpoint that enqueues the job and returns 303 to the profile, landing on `#research`. Auth-gated (admin or analyst — match "Regenerate review").
  - `app/routes/competitors.py` — new `GET /competitors/{id}/research/status` endpoint returning the status-card HTMX partial. Called by the tab's poller while a run is active.
- **Templates**
  - `app/templates/competitor_profile.html` — add `Research` tab button at the end of the tab bar, add `#tab-research` panel wrapping the status card + latest report + history.
  - `app/templates/_research_status.html` — new partial rendering the status card (idle / queued / running / ready / failed states). Single partial returned by both the initial page render and the polling endpoint; consistent UI path.
  - `app/templates/_research_report.html` — new partial rendering one report: header (run date, agent, duration, cost-if-available), body via marked.js, sources as citation chips.
- **CSS**
  - `app/static/style.css` — `.research-status`, `.research-status.running`, `.research-status.ready`, `.research-status.failed`, `.citation-chip`. Match the existing `.panel` / `.signal-card` visual language. Bump the `?v=` cache-buster on the stylesheet link.
- **JS**
  - None new. HTMX handles the poll; the existing tab-switch JS already handles `#research`.
- **No new LLM SDKs beyond the Gemini Python client, no new search providers.**

## Data model

```python
class DeepResearchReport(Base):
    """One Gemini Deep Research run for a competitor. Append-only:
    latest per competitor_id is the 'current' view, older rows are
    history surfaced as a collapsible list on the Research tab.

    Created in 'queued' state when the user clicks Run. The job flips
    to 'running' once Gemini confirms the interaction is active,
    'ready' when the final report is written, or 'failed' with the
    error message captured in `error`.
    """
    __tablename__ = "deep_research_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)

    # Gemini-side identifier, so we can reconnect to an in-flight run
    # after a server restart (poll on boot, resume the status).
    interaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # "preview" | "max". Frozen at creation time — the user's choice
    # for this run. Future Max-tier scheduled runs will store "max".
    agent: Mapped[str] = mapped_column(String(32), default="preview")

    # "queued" | "running" | "ready" | "failed" | "cancelled"
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)

    # Research brief we sent to Gemini (the filled-in skill template).
    # Persisted so we can see exactly what was asked, and so rerunning
    # with the same brief is one click away.
    brief: Mapped[str] = mapped_column(Text, default="")

    # Final report markdown. Empty until status flips to 'ready'.
    body_md: Mapped[str] = mapped_column(Text, default="")

    # Structured citations from Gemini. Shape (subject to what the
    # Interactions API actually returns — adapter normalizes):
    #   [{"title": str, "url": str, "published_at": str | None,
    #     "snippet": str | None}]
    sources: Mapped[list] = mapped_column(JSON, default=list)

    # Human-readable error when status='failed'. None otherwise.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Runtime metadata so the UI can show "6m 42s, $2.18" etc.
    # Cost is best-effort — populated from Gemini usage metadata if
    # the API returns it, otherwise null.
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Model label for auditability ("gemini-deep-research-preview-04-2026").
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
```

**Indexes:**
- `(competitor_id, started_at DESC)` — primary access pattern (latest + history for one competitor).
- `interaction_id` — for the resume-on-boot sweep.
- `status` — for the "are any runs currently in flight?" check used by the status bar.

## Execution flow

1. User lands on `/competitors/42`, clicks the **Research** tab (URL becomes `.../42#research`).
2. Tab renders:
   - If there's an in-flight run (`status IN ('queued','running')`) → status card with "Running…" + elapsed time + HTMX poller active.
   - Else if there's a latest `ready` report → render it from `_research_report.html`. A "Run new research" button appears in the tab header.
   - Else → empty state. "No deep research yet. Click Run to kick off a 5–15 minute deep dive." + Run button.
   - `GEMINI_API_KEY` missing → empty state is replaced by the "Add a key" nudge. Run button disabled.
3. User clicks **Run Deep Research**. POST `/competitors/42/research/run`. Server:
   - Creates a `Run(kind="deep_research", triggered_by="manual", status="running")`.
   - Loads the `deep_research_brief` skill, fills in placeholders from the competitor row + `config.json`.
   - Creates a `DeepResearchReport(status="queued", brief=..., run_id=...)`.
   - Enqueues `run_deep_research_job` on the existing scheduler's threadpool (same pattern as `run_competitor_scan_job`).
   - Returns 303 to `/competitors/42#research`.
4. Tab re-renders with status card in "queued" state, HTMX begins polling `/competitors/42/research/status` every 10s.
5. Job wrapper:
   - Calls `gemini_research.start_research(brief, agent="preview")`. Updates the report row: `interaction_id`, `status="running"`. Logs RunEvent "Research started (agent=preview, interaction=...)".
   - Polls Gemini every ~20s via `gemini_research.poll_research(interaction_id)`. On each tick: if the API exposes intermediate step info (search queries, pages read), write a RunEvent `level="info"` for visibility in `/runs`. These are optional — the MVP path doesn't need them.
   - On terminal success: persist `body_md`, `sources`, `finished_at`, `cost_usd`, `model`. Flip `status="ready"`. Close the `Run` with `status="ok"`. Log a `level="material"` RunEvent with `meta={competitor_id, competitor_name, report_id}` so the live log surfaces a clickable link ("Deep research ready for Indeed").
   - On terminal failure or timeout (hard cap 30 min, configurable via `DEEP_RESEARCH_TIMEOUT_S`): flip `status="failed"`, store `error`, close the `Run` with `status="error"`.
6. HTMX poll eventually sees `status="ready"` and swaps the status card for the full report partial. Polling stops.

### Resume-on-boot

App startup sweeps `DeepResearchReport` rows with `status IN ('queued','running')` and re-polls each one's `interaction_id`:
- If Gemini says it's done → persist the result, mark ready.
- If Gemini says it's still running → re-enqueue the poll job (a thin wrapper that skips the `start_research` call and jumps straight into the polling loop).
- If Gemini can't find it → mark failed with error "interaction lost on restart".

This runs in the same lifespan hook as competitor seeding so Railway release-phase failures don't leave orphan rows in "running" forever.

## Prompt (`skill/deep_research_brief.md`)

Sketch, not final:

```markdown
You are researching a competitor of {{our_company}} in {{our_industry}}.

**Competitor:** {{competitor_name}}
**Our working view of their threat angle:** {{threat_angle}}
**Category:** {{category}}

Produce a deep research report that an investor or CEO could read before a
strategy meeting. Ground every claim in cited sources.

Cover:
1. **Strategy today** — their stated positioning, recent product direction,
   revenue model (if public), key partnerships, geographies, segments.
2. **Momentum** — recent funding, hiring trends, product launches, pricing
   or packaging changes in the last 6 months. Note material 12-month drift.
3. **People** — key leaders, notable recent executive moves.
4. **How they compete with {{our_company}}** — where they overlap, where
   they don't, where the bet is {{competitor_name}} wins.
5. **Watchlist** — the 3–5 signals that would materially change our view of
   this competitor in the next 6 months.

**Focus topics we care about:** {{watch_topics}}

Prefer primary sources (earnings calls, SEC filings, official blog posts,
product changelogs, verified executive statements) over secondary news
coverage. Call out uncertainty explicitly — if a claim is speculative,
label it as such.
```

Stored as a regular skill so we can iterate it at `/settings/skills` and version it alongside the rest.

## UI

### Tab bar

Extend the tab bar in `competitor_profile.html`:

```html
<button role="tab" class="tab" data-tab="research" id="tabbtn-research"
        aria-selected="false" aria-controls="tab-research">Research</button>
```

And the JS `TABS` array: `['review', 'momentum', 'positioning', 'findings', 'research']`.

Tab lives at the end because it's the least frequently used surface — Review is daily, Research is weekly at most.

### Status card states

Renders from `_research_status.html` based on the latest row's `status`:

- **No row yet (empty state)**
  > No deep research on {{competitor.name}} yet.
  > A Deep Research run takes 5–15 minutes. Uses Gemini's web research agent.
  > [ Run Deep Research ]

- **queued**
  > Queued · just now
  > Waiting for Gemini to start…

- **running**
  > Running · 3m 42s elapsed
  > Gemini is researching — this usually takes 5–15 minutes.
  > (Progress line if the API exposes steps: "Reviewed 42 sources so far")
  > [ Cancel ]

- **ready**
  > *Report card — see below. Status card hides; report renders.*

- **failed**
  > Failed · {{error}}
  > [ Try again ]

- **cancelled**
  > Cancelled · {{when}}
  > [ Run again ]

### Report card (`_research_report.html`)

- Header row: `Deep Research · {{agent}} · {{started_at}} · {{duration}} · {{cost if present}}`.
- Body: `marked(report.body_md)`, same renderer as the Review tab.
- Sources: a grid of `.citation-chip` elements, each linking out; hover shows title + published date.
- Below the latest report: a collapsible `<details>` with prior research runs, the same pattern as Prior reviews on the Review tab. Each prior row is a one-line summary (date, agent, source count) that expands to the full `_research_report.html` body.

### Run button placement

- Latest report present → "Run new research" lives in the tab's action row, next to a "View history" affordance.
- No report yet → the Run button lives inside the empty-state card.

Confirm-on-click for the Run button: a simple `onclick="return confirm(...)"` noting the expected duration. Deep Research runs cost real money; the extra click is worth it.

## Cost & rate-limit discipline

- v1 expectation: a handful of runs per week across all competitors. Assume Preview is priced in the $1–3 range per run; Max could be $5–15. Budget visibility matters — the report card shows `cost_usd` when the API reports it, and `/runs` surfaces the same.
- **Per-competitor cooldown.** If the latest report for this competitor is `ready` and `started_at < 24h ago`, the Run button becomes a "Report is fresh (2h ago) — Run anyway?" confirm. Prevents accidental double-runs from an impatient click.
- **Global concurrency cap.** No more than `DEEP_RESEARCH_MAX_CONCURRENT=2` in-flight runs at once across the whole install (small single-process app, don't hammer Gemini). Third-click Run returns a friendly "2 research runs already running — wait for one to finish" message.
- **Hard timeout.** 30 minutes. Covers the advertised 5–20 min window + generous buffer. Beyond that we assume Gemini is stuck and we mark the row failed. The Gemini task may still complete on their side — we don't care, we already gave up.

## Auth

- The Run endpoint requires admin or analyst role, same as "Regenerate review" and the positioning refresh endpoint. Viewers can read past reports but can't kick off new ones — they cost money.
- The status poll endpoint is readable by anyone with view access to the competitor.

## Testing

- Open `/competitors/<seeded-id>#research`. With no reports, shows empty state + Run button.
- With `GEMINI_API_KEY` unset, shows "Add a key" nudge; Run button disabled; the Anthropic/Tavily-missing states are the visual template.
- Click Run. UI lands back on `#research`. Status card shows "queued". Within 30s flips to "running".
- Leave the page. Navigate back. Status is still "running" and the HTMX poller resumes.
- Stub the adapter: force a synthetic `ready` response. Status card swaps to the report card. Body renders. Citation chips link out.
- Stub a `failed` response. Failed state rendered; Try again re-runs.
- Restart the server mid-run. Boot sweep re-polls. Status card continues to update.
- Cooldown: run one, then within a minute click Run again. Confirm prompt appears with "fresh 1m ago" wording.
- Concurrency: kick off 3 runs on 3 different competitors in quick succession. Third gets the friendly error.
- `/runs` shows the `deep_research` runs with start/finish times. Live log shows the `material` event on completion, with a clickable link to the report.

## Acceptance criteria

1. `/competitors/<id>` has a **Research** tab that renders without errors on every competitor, with or without past runs, with or without `GEMINI_API_KEY` set.
2. Clicking Run enqueues a background job, writes a `DeepResearchReport` row in `queued`, and navigates back to `#research` showing the running state.
3. The HTMX status card polls at 10s (backing off to 30s after 5 min), stops polling on terminal states, and updates without a full page reload.
4. Completed reports render body_md via marked.js + sources as clickable chips; history is collapsible below the latest.
5. Missing `GEMINI_API_KEY` shows a key-missing nudge pointing at `/settings/keys`, consistent with Tavily/Anthropic missing states.
6. Failures: Gemini errors surface in the tab + the `Run`. Hard timeout at 30 min. Restart-mid-run is recoverable via boot sweep.
7. Cooldown + concurrency cap enforced with clear UI messaging.
8. Deep research runs appear on `/runs` with `kind="deep_research"`, and in the live log (`level="material"` event on success).
9. No changes to existing tabs' behavior; tab spec 02's contract is preserved (hash-driven, no new routes for switching, JS unchanged except for the extended `TABS` array).
10. CSS cache-buster bumped.

## What this unblocks

- **Scheduled Max runs.** Swap `agent="max"` and put a weekly cron on top of `run_deep_research_job` per competitor. The table is already the right shape to absorb it.
- **Multi-competitor research.** A future `/research` landing page listing latest reports across all competitors, with filters and a "research landscape" view. Same table, cross-competitor query.
- **Brief customization.** Per-competitor skill overrides (different focus areas for "pricing-war competitor" vs "positioning threat" vs "distribution threat") — once the one-size prompt proves it works, segment it.
- **Citation analytics.** Once a few weeks of reports exist, the `sources` JSON lets us answer "what domains does Gemini lean on for our space?" — both a quality signal and a candidate-source list for the regular Tavily-driven scan.
- **Feedback loop into the review synthesizer.** If Deep Research reports consistently surface something the review missed, that's evidence to adjust the review prompt or the scan keyword set. Not automated; a human read in both directions.
