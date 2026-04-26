# Spec 01 — App-store reviews as a VoC corpus

**Status:** Draft
**Owner:** Simon
**Depends on:** existing competitor model, run-queue + scheduler (`app/jobs.py`, `app/scheduler.py`), ZenRows fetcher (`app/fetcher.py`), Haiku classification path (`app/signals/llm_classify.py`).
**Unblocks:** a non-finding-shaped VoC pipeline that can later absorb Trustpilot, G2, Capterra, support-ticket exports, NPS verbatims — anywhere customers leave volume.

## Purpose

Today VoC means "Reddit threads as findings" (see `app/customer_watch.py`). Each thread becomes a row in `findings` that a human can read on its own. That model breaks for app-store reviews: any one review is too short and too noisy to be a finding, but the *aggregate* — what 200 of them say this month vs last — is some of the highest-signal customer voice we can get.

This spec adds a **separate pipeline** for app-store reviews. Individual reviews are stored as a corpus, never surfaced as findings. A periodic synthesis pass extracts a small set of **themes** per competitor (rolling state, not events) and emits a finding only when a theme **emerges** or **shifts materially**. The themes themselves live on the competitor profile as a rolling read; the findings only fire on change.

v1 ships **Apple App Store** as the only source. Google Play is spec 02 — it needs scraping rather than a public feed and the prompt + theme model should prove themselves on one store first.

## Non-goals

- **Per-review findings.** Each review is corpus, not a finding. The Reddit/Tavily VoC flow keeps doing what it does; this is a parallel surface.
- **Real-time ingest.** App-store reviews are slow-moving. Daily ingest is plenty; a missed run doesn't lose anything (RSS goes back ~500 reviews per app/country).
- **Sentiment scoring per review.** We let the synthesis LLM aggregate sentiment per theme; we don't scaffold a per-review sentiment column.
- **Reuse of `findings` for raw reviews.** Tempting (one table, one set of UI), but it muddies the model — `findings` rows are individually meaningful by contract. Keep the corpus separate.
- **Reuse of the `scanner.py` / `customer_watch.py` flow.** This is a different pipeline shape (rolling synthesis, not per-item enrichment). Living inside scanner would cost more than it saves.
- **Replying to reviews.** Out of scope. We watch; we don't engage.
- **Google Play in v1.** Deferred to spec 02. Keep the data model store-agnostic so adding it later is cheap.
- **Cross-competitor theme dedup.** Each competitor gets its own theme set in v1. Comparing themes across competitors ("3 of our 5 competitors all show a 'login broken' theme") is a market-synthesis follow-up.

## Design principles

1. **Two passes, not one.** *Ingest* (cheap, frequent, no LLM) writes raw reviews to the corpus. *Synthesise* (LLM, weekly per competitor) reads the corpus and updates themes. Decoupling them means a synthesis bug never costs us reviews and an ingest hiccup never costs us themes.
2. **Themes are rolling state, not events.** A theme is a row that exists for as long as customers are talking about it. We update `volume`, `sentiment`, `last_seen`, and the sample-quote set; we don't insert a new row each week.
3. **Findings emit on change, not on existence.** A theme produces a `Finding` row in two cases only:
   - **Emergence** — first time we've seen it.
   - **Material shift** — trailing-30-day volume changes ≥ 50% vs prior 30 days, *or* sentiment polarity flips. (Thresholds tuneable in `config.json`.)
   Stable themes are visible on the profile but don't spam the inbox.
4. **Source-agnostic schema, source-specific adapter.** `app_reviews` doesn't care that Apple is the first store. The fetcher lives in `app/adapters/app_stores/apple.py` and returns a normalised `ReviewRow` dataclass. Adding Play later = a new adapter file + a `store='play'` branch in the orchestrator.
5. **App Store RSS, not iTunes Search API for ingest.** Apple publishes a public, key-free RSS feed of reviews per app/country/page. Up to 50 reviews per page, 10 pages → ~500 reviews. No auth, no quota in practice. ZenRows is overkill here; use a plain `httpx.get` with a long User-Agent. Reserve ZenRows for Play (spec 02) where the page is rendered.
6. **Source config is per-competitor, explicit.** No magic mapping from competitor name → app id. Admin enters the App Store id and country code on the competitor edit page; we store one `AppReviewSource` row per (competitor, store, country). A competitor can have multiple sources (e.g. iOS-AU and iOS-US) — they aggregate into one theme set per competitor.
7. **Synthesis is per-competitor, single Haiku call.** Last N=200 reviews (chronological, latest first) + current themes (compact JSON) → updated theme set + diff classification per theme (`new` / `same` / `shifted` / `dropped`). One round trip; ≤ ~$0.02 per competitor per week. Aligns with the existing `feedback_llm_classification` memory: Haiku one-call enrich.
8. **Defence in depth on dedup.** App Store review ids are stable. Hash on `(store, store_review_id)` with a uniqueness constraint. RSS sometimes returns slightly different bodies for the same id (translation tweaks); store_review_id wins.
9. **Cap everything.** Sources per competitor: 5. Reviews ingested per source per run: 200. Themes per competitor: 8. These caps are the difference between "useful and cheap" and "an AWS bill."
10. **No new search providers, no new LLM SDKs.** Anthropic SDK + the existing `httpx` we already have.

## Where it lives

- **Models + migration**
  - `app/models.py` — `AppReviewSource`, `AppReview`, `ReviewTheme`.
  - `alembic/versions/<new>_app_reviews_corpus.py` — three tables + indexes below.
- **Adapters**
  - `app/adapters/app_stores/__init__.py` — exports `fetch_apple(app_id: str, country: str, max_pages: int = 10) -> list[ReviewRow]` and the `ReviewRow` dataclass.
  - `app/adapters/app_stores/apple.py` — RSS fetch + parse. ~80 LoC. Returns normalised rows; no DB writes.
- **Ingest pass**
  - `app/app_reviews.py` — `ingest_for_competitor(comp: Competitor, db) -> int` (returns rows inserted) and `ingest_all(db) -> dict[str, int]`. No LLM, no Run wrapper internals — just the work.
- **Synthesis pass**
  - `app/voc_themes.py` — `synthesise_for_competitor(comp: Competitor, db) -> ThemeSyntheseResult`. Loads last 200 reviews + current themes, calls Haiku, persists updated themes, computes deltas, emits `Finding` rows for emergence/shift.
- **Skill**
  - `skill/voc_theme_synthesise.md` — prompt template (competitor name, current themes JSON, reviews list → updated themes + diff).
  - `app/skills.py::KNOWN_SKILLS` — register.
- **Jobs**
  - `app/jobs.py` — `run_ingest_app_reviews_job(triggered_by="schedule")` (creates `Run(kind="ingest_app_reviews")`, calls `ingest_all`, logs material event with `{rows_inserted}`).
  - `app/jobs.py` — `run_voc_themes_job(competitor_id: int | None = None, triggered_by="schedule")`. None = sweep all active competitors with ≥ 1 source; competitor_id = run for one. One `Run(kind="synthesise_voc_themes")` per call (one row covers a sweep, not per competitor — keeps the runs page readable).
- **Scheduler**
  - `app/scheduler.py` — daily cron at `scan_hour - 2` for ingest (well clear of the main scan); weekly cron Tuesday 04:00 local for synthesis (after Monday's market synthesis so theme deltas can feed next week's market view).
- **Routes**
  - `app/routes/competitors.py` — `POST /admin/competitors/{id}/app-sources` (add a source: store, app_id, country). `POST /admin/competitors/{id}/app-sources/{src_id}/delete`.
  - `app/routes/competitors.py` — `POST /admin/voc/ingest/run`, `POST /admin/voc/themes/run` (manual triggers, admin only).
  - `app/ui.py::competitor_profile` — load the competitor's themes (rolling state, ordered by volume desc) and pass to template; load a paginated review sample on the App reviews tab.
- **Templates**
  - `app/templates/competitor_profile.html` — new "App reviews" tab that renders `_app_review_themes.html` + `_app_review_samples.html`.
  - `app/templates/_app_review_themes.html` — partial: theme cards (label, sentiment chip, volume sparkline, sample quotes, last_seen).
  - `app/templates/_app_review_samples.html` — partial: paginated raw review list, filterable by theme.
  - `app/templates/admin_competitor_edit.html` — new "App stores" section: list current sources + add-source form (store dropdown, app_id, country code).
- **CSS**
  - `app/static/style.css` — `.theme-card`, `.theme-card .sentiment-pos|.neg|.mixed`, `.review-sample`, `.app-source-row`. Cache-buster bump.
- **Config**
  - `config.json` — new `app_reviews` block: `{ "enabled": true, "ingest_max_pages": 10, "synthesise_max_reviews": 200, "shift_threshold_pct": 50, "max_themes_per_competitor": 8 }`.
- **No changes to** `findings` schema, `scanner.py`, `customer_watch.py`, or the existing run pipeline beyond a new kind value.

## Data model

```python
class AppReviewSource(Base):
    """A single (competitor, store, app_id, country) tuple we ingest from.
    A competitor can have multiple — typically one per country. v1 supports
    store='apple' only; the column exists so spec 02 can add 'play' without
    a migration."""
    __tablename__ = "app_review_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("competitors.id"), index=True
    )
    store: Mapped[str] = mapped_column(String(16))           # "apple" | "play"
    app_id: Mapped[str] = mapped_column(String(64))           # numeric for Apple, package name for Play
    country: Mapped[str] = mapped_column(String(8), default="us")  # ISO 3166-1 alpha-2
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ingested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ingested_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("store", "app_id", "country", name="uq_app_source"),
        Index("ix_app_source_competitor", "competitor_id", "enabled"),
    )


class AppReview(Base):
    """Raw review corpus. Append-only after ingest; no in-place updates.
    Reviews never become findings — only themes do."""
    __tablename__ = "app_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("app_review_sources.id"), index=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)
    store: Mapped[str] = mapped_column(String(16))
    store_review_id: Mapped[str] = mapped_column(String(128))     # Apple's "id" field
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1–5
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lang: Mapped[str | None] = mapped_column(String(8), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    # Set by the synthesis pass when this review was used as a sample for a
    # theme. NULL means "not classified yet" — useful for backfill jobs and
    # for the "uncovered reviews" diagnostic.
    theme_id: Mapped[int | None] = mapped_column(
        ForeignKey("review_themes.id"), nullable=True, index=True
    )

    __table_args__ = (
        UniqueConstraint("store", "store_review_id", name="uq_review_storeid"),
        Index("ix_review_competitor_posted", "competitor_id", "posted_at"),
    )


class ReviewTheme(Base):
    """Rolling state per (competitor, theme). Updated in-place by the
    synthesis pass. Volume and sentiment are recomputed every run."""
    __tablename__ = "review_themes"
    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("competitors.id"), index=True
    )
    # Short noun phrase, e.g. "login fails after iOS 17 update". Stable
    # across runs unless the synthesis pass renames it (rare; see prompt).
    label: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    # "positive" | "negative" | "mixed" — single label assigned by the LLM.
    sentiment: Mapped[str] = mapped_column(String(16), default="mixed")
    # Trailing-30-day count, recomputed each run.
    volume_30d: Mapped[int] = mapped_column(Integer, default=0)
    # Prior 30-day count (the window before the current 30d), so the UI
    # can render trend arrows and the emit-finding rule has its inputs.
    volume_prev_30d: Mapped[int] = mapped_column(Integer, default=0)
    # Sample review ids the LLM picked as illustrative. Stored as JSON
    # because the count varies (typically 3–5).
    sample_review_ids: Mapped[list] = mapped_column(JSON, default=list)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # "active" | "dormant" — dormant when volume_30d == 0 for 2+ runs.
    # Dormant themes are kept (history matters) but hidden by default.
    status: Mapped[str] = mapped_column(String(16), default="active")
    # The Run that last updated this theme. Useful for "what changed in
    # the last synthesis" debugging.
    last_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)

    __table_args__ = (
        Index("ix_theme_competitor_status", "competitor_id", "status"),
    )
```

**Indexes:**
- `app_reviews(competitor_id, posted_at DESC)` — primary access pattern for "show me the last 200 reviews for synthesis" and the samples panel.
- `app_reviews(store, store_review_id)` UNIQUE — dedup on ingest.
- `review_themes(competitor_id, status)` — the profile-tab read.

## Execution flow

### Ingest pass (daily, 06:00)

1. Cron fires `run_ingest_app_reviews_job`. Creates `Run(kind="ingest_app_reviews", triggered_by="schedule")`.
2. For each `AppReviewSource` where `enabled = TRUE`:
   - `apple.fetch_apple(app_id, country, max_pages=10)` returns up to ~500 normalised `ReviewRow`s (latest first).
   - Insert each, skipping duplicates via the `(store, store_review_id)` unique constraint. Count inserts.
   - On any HTTP/parse error: set `last_error` on the source row, continue to the next source. Don't fail the whole run for one bad source.
   - Update `last_ingested_at`, `last_ingested_count`.
3. Write a `material` RunEvent: `{sources_processed, rows_inserted, rows_skipped_dedup, errors}`.
4. Close the Run as `ok` (or `error` only if every source failed).

### Synthesis pass (weekly, Tuesday 04:00)

1. Cron fires `run_voc_themes_job(competitor_id=None)`. Creates `Run(kind="synthesise_voc_themes")`.
2. For each active competitor that has ≥ 1 enabled source AND ≥ 10 reviews ingested in the last 60 days (skip the long tail — synthesis on 4 reviews is noise):
   - Load the last 200 reviews (chronological, latest first), each as `{id, rating, posted_at, body}`.
   - Load current `ReviewTheme` rows for this competitor (`{id, label, description, sentiment, volume_30d, sample_review_ids}`).
   - Render `voc_theme_synthesise` skill with `{competitor_name, current_themes_json, reviews_json}`.
   - Single Haiku call; expects a JSON object: `{ "themes": [...], "diff": [...] }` (shape below).
   - Reconcile:
     - `diff[*].kind = "new"` → insert `ReviewTheme(first_seen=now)`, emit `Finding(signal_type="voc_theme", payload={kind: "emerged", theme_id, label, sample_quotes})`.
     - `diff[*].kind = "same"` → update existing row's `volume_30d`, `volume_prev_30d`, `sentiment`, `sample_review_ids`, `last_seen`. No finding.
     - `diff[*].kind = "shifted"` → as `same`, plus emit `Finding(signal_type="voc_theme", payload={kind: "shifted", theme_id, prior_volume, new_volume, prior_sentiment, new_sentiment})` if the shift clears the threshold (`shift_threshold_pct` or sentiment polarity flip).
     - `diff[*].kind = "dropped"` → flip the row to `status="dormant"`. No finding (low signal).
   - Backfill `app_reviews.theme_id` for the reviews the LLM cited as samples (so the samples-panel filter works).
3. Write a `material` RunEvent per competitor: `{competitor_id, themes_total, new, shifted, dropped, findings_emitted}`.
4. Close the Run.

### Manual triggers

- `POST /admin/voc/ingest/run` — admin button on `/runs` (or on the competitor profile App reviews tab) to force an ingest now. Same job, `triggered_by="manual"`.
- `POST /admin/voc/themes/run?competitor_id=N` — force synthesis for one competitor (skips the 10-review threshold; admin opt-in).

### Concurrency

- One `ingest_app_reviews` Run in flight at a time across the whole install. Same for `synthesise_voc_themes`. The drainer's existing single-instance discipline (`max_instances=1`) handles this — no new locks.
- Ingest and synthesis can run concurrently with each other and with the main scan; they touch different tables.

## Prompt (`skill/voc_theme_synthesise.md`)

Sketch:

```markdown
You are a customer-research analyst summarising recent app-store reviews for {{competitor_name}}.

You will receive:
- Up to N recent reviews, latest first, each with {id, rating (1–5), posted_at, body}.
- The current set of themes we've previously identified for this competitor (may be empty).

Your job: produce an updated set of up to 8 themes that capture what these
reviews are saying, and classify how each theme has changed since the
previous run.

A theme is a short noun phrase (≤ 80 chars) describing one specific issue,
delight, or pattern. Good: "login screen freezes after iOS 17 update".
Bad: "negative experiences" (too vague), "users hate the app" (not a
theme, that's a sentiment).

For each theme, return:
- label: the noun phrase (≤ 80 chars).
- description: 1–2 sentences explaining what customers are saying. Quote
  reviewer language where possible.
- sentiment: "positive" | "negative" | "mixed".
- volume_30d: integer count of reviews from the last 30 days that touch
  this theme. (You will be given posted_at on each review — count them.)
- volume_prev_30d: integer count from the 30 days before that.
- sample_review_ids: 3–5 ids of the most illustrative reviews. Choose
  ones a human can read in 30 seconds and immediately understand the theme.

For the diff, return one entry per theme in your output, with kind:
- "new" — wasn't in the current themes input.
- "same" — was there before, no material change in language or volume.
- "shifted" — was there before, but volume/sentiment has changed enough
  that a human should re-read it.
- "dropped" — was in the current themes input but not in your output.
  Include a `theme_id` referencing the current theme; for new/same/shifted
  the theme_id will be filled by the caller.

**Stability discipline.** If a theme from the current input is still
present in the reviews, prefer keeping the same `label` and `description`
verbatim. Renaming themes between runs makes trends unreadable. Only
rename if the prior label is genuinely wrong now.

**Don't invent themes** that are only in 1–2 reviews. Volume matters —
if you can't point to ≥ 5 reviews supporting a theme, leave it out.

## Input

Current themes:
{{current_themes_json}}

Recent reviews:
{{reviews_json}}

## Output format

Respond with ONLY a JSON object, no prose, no markdown fences:

{
  "themes": [
    {
      "label": "...",
      "description": "...",
      "sentiment": "positive" | "negative" | "mixed",
      "volume_30d": 0,
      "volume_prev_30d": 0,
      "sample_review_ids": ["...", "..."]
    }
  ],
  "diff": [
    { "label": "...", "kind": "new" | "same" | "shifted" | "dropped", "current_theme_id": null | int }
  ]
}
```

## UI

### Competitor profile — App reviews tab

```
┌─ Themes (last 30 days) ─────────────────────────────────────┐
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Login fails after iOS 17 update      [negative]  ▲ 142% │ │
│ │ "Won't let me past the loading screen since I updated…" │ │
│ │ Volume: 24 (was 10)  ·  Last seen: 2d ago               │ │
│ │ Samples: ★1 ★1 ★2 ★1 ★1                  [ View 24 ▸ ]   │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Resume parser accuracy improved      [positive]  ▲ 60%  │ │
│ │ "Finally pulls the right job titles, big improvement…"  │ │
│ │ Volume: 16 (was 10)  ·  Last seen: 1d ago               │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ...                                                         │
└─────────────────────────────────────────────────────────────┘

┌─ Recent reviews ────────────────────────────────────────────┐
│ Filter: [All themes ▾] [All ratings ▾] [Country: AU ▾]      │
│ ★1 · 2d ago · "Won't let me past the loading screen…"       │
│   theme: Login fails after iOS 17 update                    │
│ ★5 · 2d ago · "Finally pulls the right job titles…"         │
│   theme: Resume parser accuracy improved                    │
│ ...                                  [Load more]            │
└─────────────────────────────────────────────────────────────┘
```

- Theme card: label, sentiment chip, volume + delta (▲/▼/—), 1-line quote pulled from `description`, sample-rating row, "View N" deep-links into the samples panel filtered by `theme_id`.
- Trend arrows: ▲ if `volume_30d > volume_prev_30d`, ▼ if less, — if equal/0. Percent only shown if prior > 0.
- Dormant themes hidden behind a `<details>Show 3 dormant themes</details>` strip at the bottom.
- Empty state (no sources configured): "Track app reviews for {{competitor.name}} — [Add an App Store source](edit#app-sources)."
- Empty state (sources configured but < 10 reviews ingested): "Ingested 4 reviews so far. Themes appear after we have ≥ 10."

### Findings inbox

Theme emergence/shift findings appear with `signal_type="voc_theme"`. The card title is the theme label; the body is the description; an inline pill says "emerged" or "▲ 142% volume". Click → competitor profile → App reviews tab, scrolled to that theme.

### Competitor edit — App stores section

```
┌─ App stores ────────────────────────────────────────────────┐
│ Apple App Store                                             │
│  · 123456789 (US) · last ingested 2h ago · 47 reviews       │
│      [Disable]  [Delete]                                    │
│  · 123456789 (AU) · last ingested 2h ago · 12 reviews       │
│      [Disable]  [Delete]                                    │
│                                                             │
│ Add source:                                                 │
│  Store: [Apple ▾]   App id: [_______]   Country: [us]       │
│                                          [ Add ]            │
└─────────────────────────────────────────────────────────────┘
```

- Store dropdown shows only Apple in v1 (Play disabled with a "coming soon" tooltip).
- App-id help text: "Numeric id from the App Store URL — e.g. for `apps.apple.com/us/app/foo/id123456789`, the id is `123456789`."
- Validation on add: hit the RSS endpoint with `max_pages=1`; if it 404s or returns no entries, surface "Couldn't find any reviews for that app id in {{country}}. Check the id and country code."

## Cost & rate-limit discipline

- **Ingest:** zero LLM cost, zero search-provider cost. Apple RSS is free and unauthenticated. Per-source latency dominated by 10 sequential page fetches — ~5–10s per source. 20 competitors × 2 sources × 10s ≈ 7 min total wall time, fine for an off-hour cron.
- **Synthesis:** Haiku, single call per competitor, ~6k input tokens (200 reviews × ~30 tokens) + ~1.5k output. Per-competitor cost ~$0.01–0.03. 20 competitors weekly = ~$0.50/wk.
- **Caps in adapter, not in prompt:** truncate reviews list to 200 before rendering the prompt; truncate themes output to 8 before persisting. The prompt asks for ≤ 8 but we don't trust it.
- **Per-source rate-limit:** sleep 500ms between Apple RSS pages. The endpoint is generous but politeness is cheap.

## Auth

- Configuring an app source: admin only (same as editing other competitor fields).
- Manual ingest / synthesis triggers: admin only.
- Viewing themes + samples on the competitor profile: any authenticated user (same as the rest of the profile).

## Testing

- **Empty install:** no sources configured. Daily ingest cron runs, completes in <1s, RunEvent says `0 sources, 0 rows`. App reviews tab on every competitor shows the "Add a source" empty state.
- **Add a known-good source:** enter Apple id + `us` for a competitor. Validation hits the RSS endpoint and accepts. First ingest pulls ~50–500 reviews. Tab now shows "Ingested N reviews so far. Themes appear after ≥ 10" if N < 10.
- **Add a known-bad source:** id `0000000`. Validation surfaces the friendly error; row is not inserted.
- **Dedup:** run ingest twice in the same day. Second run inserts 0 new rows (UNIQUE constraint catches every one).
- **First synthesis:** competitor with 200+ reviews, no current themes. Synthesis returns ≤ 8 themes, all `kind="new"`. ≤ 8 `Finding(signal_type="voc_theme", payload.kind="emerged")` rows inserted.
- **Stable second run:** trigger synthesis again, no new reviews since. All themes return as `kind="same"`. Zero findings emitted. Theme rows updated in place (not duplicated).
- **Material shift:** seed reviews so a theme's volume_30d goes from 5 → 15 (+200%). Synthesis returns it as `shifted`. One `Finding(signal_type="voc_theme", payload.kind="shifted")` emitted. Profile tab shows ▲ 200%.
- **Sentiment flip:** a previously-positive theme's sentiment turns negative. One `shifted` finding emitted regardless of volume change.
- **Drop:** a theme with no supporting reviews this run. Returned as `dropped`. Row flips to `status="dormant"`, hidden by default on the profile.
- **One-source failure:** point one source's app_id at an invalid id. Other sources ingest cleanly; failed source has `last_error` set; Run completes `ok`.
- **Manual run:** admin clicks "Run synthesis now" on a competitor with 12 reviews. Synthesis runs; produces 1–2 themes; profile updates within a couple of seconds (HTMX swap).
- **`/runs` page:** ingest and synthesis runs appear with their `kind` and material event counts.

## Acceptance criteria

1. Three new tables (`app_review_sources`, `app_reviews`, `review_themes`) are created via Alembic migration with the indexes specified.
2. Admins can add/disable/delete an Apple App Store source per competitor at `/admin/competitors/{id}/edit`. Validation rejects unknown app ids before persisting.
3. A daily scheduled `run_ingest_app_reviews_job` ingests reviews from every enabled source, dedupes on `(store, store_review_id)`, and writes a material RunEvent.
4. A weekly scheduled `run_voc_themes_job` runs synthesis for every active competitor with ≥ 10 reviews in the last 60 days, persists themes, and emits `Finding(signal_type="voc_theme")` only on emergence or material shift (configurable threshold).
5. The competitor profile has an "App reviews" tab showing themes (rolling state) above a paginated raw-review samples panel.
6. Findings emitted by synthesis appear in the existing inbox with the new signal type, deep-link to the App reviews tab.
7. No raw review is ever written to the `findings` table.
8. The system handles a single failing source without failing the whole ingest run.
9. CSS cache-buster bumped.

## What this unblocks

- **Spec 02 — Google Play.** Same model, new adapter using ZenRows for the rendered page. The store column is already in place.
- **Spec 03 — Trustpilot / G2 / Capterra.** Same shape: new adapter, same `app_reviews`/`review_themes` flow (renamed table or split is a coin-flip; revisit when we have a second source live).
- **Cross-competitor theme view on the market dashboard.** Once 5+ competitors have themes, ask the market-synthesis pass to identify shared themes ("3 of 5 competitors all show 'pricing too high' as a top theme") — pure read, no new ingest.
- **Theme → keyword feedback loop.** A high-volume theme like "AI screening accuracy" is a strong hint for the keyword tuner: surface it on the Optimise screen as a suggested watchlist keyword.
- **Sentiment-shift digest.** A weekly email of "themes that flipped polarity this week across your watchlist" — directly composable from the existing `Finding(signal_type="voc_theme")` rows; no new pipeline.
