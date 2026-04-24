# Spec 01 — Discover competitors (Manage Watchlist)

**Status:** Draft
**Owner:** Simon
**Depends on:** existing competitor autofill (`app/competitor_autofill.py`) and the New Competitor form at `/admin/competitors/new`.
**Unblocks:** turning Manage Watchlist into an *active* surface rather than a data-entry screen; periodic "who's new in our space" sweeps; a future signal that flags when a scanned finding mentions a not-yet-tracked company.

## Purpose

Today the only way to add a competitor is to already know its name, type it into `/admin/competitors/new`, and let autofill populate the rest. That works once you know who you're watching — it doesn't help you notice you should be watching someone new. The market moves; the list stagnates.

This spec adds a **Discover** panel to `/admin/competitors` that runs a tool-use loop (the `competitor_autofill` pattern, re-pointed at the discovery problem) to surface a ranked list of candidate competitors we aren't already tracking. Each candidate shows a name, homepage, one-line "why they matter," and evidence links. One click sends the user straight into the existing New Competitor form pre-seeded with the name — the autofill machinery takes it from there.

Discovery is admin-triggered, cheap enough to run weekly-ish, and never auto-adds anyone. The human is always the gate.

## Non-goals

- **Auto-adding competitors.** Every candidate requires an explicit human click. We never silently grow the watchlist.
- **A separate scan pipeline.** No new cron, no scheduler integration in v1. Admin clicks Run; a single tool-use loop produces a list; done.
- **Cross-checking candidates against finding history.** A "has this company appeared in our findings text?" hint would be nice but is a second-spec feature — it requires scanning `findings.summary` for each candidate and isn't load-bearing for v1.
- **Bulk Add.** One candidate at a time, because each one goes through the autofill form for review. Bulk adoption invites sloppy data.
- **Streaming the tool-use loop to the UI like autofill does.** v1 renders the list after the loop finishes. The autofill SSE pattern is available if we want it later; not needed for this surface.
- **Replacing the existing New Competitor flow.** Adopt = deep-link into the existing form. Zero duplication of the autofill machinery.

## Design principles

1. **Reuse the autofill tool-use shape.** Same model (`claude-sonnet-4-6`), same tools (`search_web`, `fetch_url`), same `MAX_TOOL_ROUNDS` budget. Different system prompt, different output schema. Ship a new adapter module; don't bolt onto `competitor_autofill.py`.
2. **Candidates persist.** A tool-use run costs real money (Anthropic + Tavily); throwing the results away on reload is wasteful. Rows live in a `competitor_candidates` table with status = `suggested` / `dismissed` / `adopted`.
3. **Dismissals are sticky.** A dismissed candidate is *excluded from future discovery runs by homepage domain*. If we keep surfacing "Greenhouse" every week after the user said no, the tool is noise. Exclusion list = active competitors ∪ dismissed candidates (by `homepage_domain`), both passed into the prompt.
4. **Adopt = deep-link, not data copy.** Clicking "Add" navigates to `/admin/competitors/new?candidate_id=N`. The form reads the candidate and pre-fills the name (and homepage as a seed hint the autofill agent can verify). On successful save, the candidate flips to `adopted` with `adopted_competitor_id` set.
5. **One skill, one file.** `skill/discover_competitors_brief.md` holds the prompt template (company, industry, existing watchlist, dismissed list, optional user hint → research brief). Editable at `/settings/skills`, versioned like everything else.
6. **Run = `Run` row.** Reuse `Run` / `RunEvent` with `kind="discover_competitors"`, `triggered_by="manual"`. Shows up on `/runs`; cost attribution works for free; live log can surface "4 new candidates" on completion.
7. **Cheap, not free.** A single discovery run should stay under ~$0.25. Cap `MAX_TOOL_ROUNDS = 10`, cap candidates returned at 8, cap per-candidate `fetch_url` confirmations. The prompt tells the model to prefer breadth (finding candidates) over depth (deep profiling each one — that's autofill's job).
8. **Fail soft.** Missing `ANTHROPIC_API_KEY` or `TAVILY_API_KEY` → panel shows the "Add a key" nudge pointing at `/settings/keys`, same pattern as the Deep Research tab.
9. **No changes to the existing Manage Watchlist tables.** The Discover panel sits above the Active / Inactive tables. Current flows (edit, delete, restore, `+ New competitor`) are untouched.

## Where it lives

- **Model + migration**
  - `app/models.py` — new `CompetitorCandidate` class (shape below).
  - `alembic/versions/<new>_competitor_candidates.py` — create the table with the indexes below.
- **Discovery loop**
  - `app/competitor_discover.py` — new module. `discover(company, industry, existing: list[str], dismissed: list[str], hint: str | None) -> list[dict]`. Tool-use loop; same `search_web` / `fetch_url` helpers (import from `competitor_autofill` or extract to `app/adapters/llm_tools.py` in a follow-up). Returns normalized candidate dicts: `{name, homepage_domain, category, one_line_why, evidence: [{url, title}]}`.
- **Prompt**
  - `skill/discover_competitors_brief.md` — Markdown prompt template with placeholders for company, industry, watchlist, dismissed list, optional user hint.
  - `app/skills.py::KNOWN_SKILLS` — register the skill.
- **Job**
  - `app/jobs.py` — new `run_discover_competitors_job(hint: str | None, triggered_by: str = "manual")`. Creates a `Run(kind="discover_competitors")`, builds the exclusion list from DB, calls the discover loop, persists rows, writes a `material` RunEvent on completion ("Discovered 4 candidates"), closes the Run.
- **Routes**
  - `app/ui.py::admin_competitors` — load `suggested` candidates (status='suggested', most recent run first), active discovery run if any, last-run timestamp. Pass to template.
  - `app/routes/competitors.py` — `POST /admin/competitors/discover/run` (enqueues the job, 303 to `/admin/competitors#discover`). Reads optional `hint` form field.
  - `app/routes/competitors.py` — `POST /admin/competitors/candidates/{id}/dismiss` (sets status=dismissed, returns HTMX-removal markup).
  - `app/routes/competitors.py` — `GET /admin/competitors/discover/status` (HTMX partial for the polling panel while a run is active).
  - `app/ui.py::admin_competitor_new` — accept `?candidate_id=N`; if set, load the candidate and pass it to the template so the Name field pre-fills and the page auto-fires the autofill stream.
  - Save handler for `/admin/competitors/new` — if a `candidate_id` was present and the save succeeds, flip the candidate to `adopted` with `adopted_competitor_id` set.
- **Templates**
  - `app/templates/admin_competitors.html` — add a `#discover` panel above the Active table: Run form (optional hint input + button), status card, candidate list.
  - `app/templates/_discover_status.html` — partial for idle / running / failed states of the run (like `_research_status.html`).
  - `app/templates/_discover_candidate.html` — partial for one candidate row (name, homepage link, category chip, one_line_why, evidence chips, Add / Dismiss buttons).
  - `app/templates/admin_competitor_edit.html` — when `candidate` is in the context, include a hidden `candidate_id` input and pre-fill the Name field; a small inline callout "Added from discovery run on {{date}} — one-line why: …"
- **CSS**
  - `app/static/style.css` — `.discover-panel`, `.candidate-card`, `.candidate-card .evidence`, `.candidate-card .actions`. Reuse `.panel` / `.citation-chip` language. Cache-buster bump.
- **No new Python SDKs, no new search providers.**

## Data model

```python
class CompetitorCandidate(Base):
    """A competitor-shaped thing surfaced by a discovery run that the user
    hasn't yet decided on. Append-only; status transitions are the only
    mutation. Primary key of identity is (homepage_domain) — we dedupe
    against this at discovery time.
    """
    __tablename__ = "competitor_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(255))
    # Canonical apex domain — lower-cased, no scheme, no www. Used for
    # dedup against competitors.homepage_domain and against other
    # candidates. Nullable only because the agent might surface a name
    # without a confirmed homepage; we still store it but can't dedupe.
    homepage_domain: Mapped[str | None] = mapped_column(String(255), index=True)

    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    one_line_why: Mapped[str] = mapped_column(Text, default="")

    # [{"url": str, "title": str}] — links the agent cited as evidence.
    # Capped at ~5 entries in the adapter.
    evidence: Mapped[list] = mapped_column(JSON, default=list)

    # "suggested" | "dismissed" | "adopted"
    status: Mapped[str] = mapped_column(String(16), default="suggested", index=True)

    # The user hint that shaped this run (optional). Persisted so the UI
    # can show "from 'focus on Australian ATS players' run".
    run_hint: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set when status='adopted' and the New Competitor save succeeded.
    adopted_competitor_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitors.id"), nullable=True, index=True
    )

    # When the user dismissed it, and why (freeform, optional).
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
```

**Indexes:**
- `(status, created_at DESC)` — primary access pattern for the Discover panel ("show me the most recent suggestions").
- `homepage_domain` — dedup lookups.

## Execution flow

1. Admin lands on `/admin/competitors`. The Discover panel at the top shows:
   - Pending candidates (status='suggested'), grouped by run, newest run first.
   - Last run: "2h ago · 4 candidates · Simon" (or "Never").
   - Run form: optional hint text input + "Run discovery" button.
   - If a run is in flight, the form is replaced by a status card with HTMX polling `/admin/competitors/discover/status` every 10s.
2. Admin enters an optional hint (e.g. "focus on Australian ATS players") and clicks Run. POST `/admin/competitors/discover/run`:
   - Creates `Run(kind="discover_competitors", triggered_by="manual", status="running")`.
   - Enqueues `run_discover_competitors_job(hint)` on the existing threadpool.
   - Returns 303 to `/admin/competitors#discover`.
3. Job wrapper:
   - Builds `existing = [c.homepage_domain for c in active competitors]`.
   - Builds `dismissed = [c.homepage_domain for c in candidates where status='dismissed']`.
   - Renders the `discover_competitors_brief` skill with `{company, industry, existing, dismissed, hint}`.
   - Calls `competitor_discover.discover(...)` — tool-use loop runs, typically 4–8 rounds, returns up to 8 candidates.
   - For each candidate: normalize `homepage_domain`; skip if it matches any existing or dismissed domain (defence in depth — the prompt already excludes them, but the model isn't reliable); insert a `CompetitorCandidate` row with status='suggested' and `run_id` set.
   - Writes a `material` RunEvent: "Discovered N candidates" with `meta={run_id, count}`.
   - Closes the `Run` as `ok`.
4. Admin re-renders (HTMX poll finishes → swap status card for candidate list). Each candidate renders as `_discover_candidate.html` with:
   - Header: name, homepage as a small chip, category tag.
   - Body: one_line_why.
   - Evidence: up to 5 chips, each a clickable link to the cited URL.
   - Actions: **Add** (navigates to `/admin/competitors/new?candidate_id=N`), **Dismiss** (hx-post to `/admin/competitors/candidates/{id}/dismiss`, optional textarea for reason, swaps the row out).
5. Clicking **Add**:
   - Navigates to `/admin/competitors/new?candidate_id=N`.
   - Form pre-fills Name from the candidate. A small callout shows "Added from discovery run — they said: {one_line_why}".
   - The existing autofill stream auto-fires (same trigger the form uses today when a name is present), populating the remaining fields.
   - Admin reviews and saves. On successful save, the save handler flips the candidate to `status='adopted'` + `adopted_competitor_id` set.
   - If the admin navigates away without saving, the candidate stays `suggested` — visible again on the Discover panel until dismissed or adopted.

### Handling a mid-run reload

If the admin reloads `/admin/competitors` while a run is active, the page reads the in-flight `Run` and renders the status card + poller. No explicit resume-on-boot logic needed for v1: discovery runs are short (~30–60s) and rare; if the server restarts mid-run, the `Run` lands in an odd state — acceptable for v1. A future pass can add a boot sweep like Deep Research has.

## Prompt (`skill/discover_competitors_brief.md`)

Sketch, not final:

```markdown
You are a competitive-intelligence analyst for {{our_company}} in {{our_industry}}.

Your job: find up to 8 companies that we should consider adding to our
competitor watchlist, that we are NOT already tracking. Use search_web
and fetch_url to discover candidates and confirm each one is a real,
operating business.

**Already tracked (do not return these):**
{{existing_list}}

**Previously dismissed (do not return these either — the human has said no):**
{{dismissed_list}}

{{#hint}}**Focus:** {{hint}}{{/hint}}

For each candidate, produce:
- name: the company's common name.
- homepage_domain: canonical apex domain (e.g. 'linkedin.com', not
  'www.linkedin.com'). MUST be verified with fetch_url.
- category: one of ["job_board", "ats", "labour_hire", "adjacent", "other"].
- one_line_why: one sentence on why {{our_company}} should watch them.
  Concrete. "They launched an AI-screening product in March targeting
  mid-market recruiters" beats "They operate in the same space."
- evidence: up to 5 {title, url} entries — search or fetch results that
  support the claim. Prefer primary sources (their own site, press
  releases, app store listings) over aggregators.

Prefer breadth over depth. Don't spend the whole budget profiling one
candidate — surface several and let the human decide which to investigate.

Do NOT return anything speculative. Every candidate must be a real
company with a verifiable homepage. If you can't find 8 good ones,
return fewer.

## Output format

Respond with ONLY a JSON object of this shape (no prose, no markdown fences):
{
  "candidates": [
    {
      "name": "...",
      "homepage_domain": "...",
      "category": "...",
      "one_line_why": "...",
      "evidence": [{"title": "...", "url": "..."}]
    }
  ]
}
```

## UI

### Discover panel (on `/admin/competitors`, above the Active table)

**Idle state (no active run):**
```
┌─ Discover new competitors ─────────────────────────┐
│ Last run: 2h ago · 4 candidates · [View runs]     │
│                                                    │
│ Optional focus: [_____________________________]    │
│                                                    │
│                             [ Run discovery ]      │
└────────────────────────────────────────────────────┘

┌─ 4 suggested candidates ────────────────────────────┐
│ ┌────────────────────────────────────────────────┐ │
│ │ Greenhouse  ·  greenhouse.io  ·  [ats]        │ │
│ │ Mid-market ATS rolling out AI sourcing in Q2. │ │
│ │ Evidence: [TechCrunch] [product blog] [...]   │ │
│ │                   [ Add ]  [ Dismiss ]        │ │
│ └────────────────────────────────────────────────┘ │
│ ...                                                │
└────────────────────────────────────────────────────┘
```

**Running state:**
```
┌─ Discover new competitors ─────────────────────────┐
│ Running · 23s elapsed · researching...             │
│ (This usually takes 30–90 seconds.)                │
└────────────────────────────────────────────────────┘
```

**Failed state:**
```
Failed · {{error}}  [ Try again ]
```

**Missing-key state** (same shape as Deep Research):
```
Discovery uses Anthropic + Tavily. Add keys at /settings/keys to enable.
```

### Candidate card

- Header row: name (bold), homepage chip (linkable), category chip.
- Body: one_line_why.
- Evidence chips: up to 5 `<a>` elements styled as chips, hover shows title.
- Action row: primary **Add** button (anchor to `/admin/competitors/new?candidate_id=N`), secondary **Dismiss** button (hx-post; opens a small reason textarea inline; Enter submits).

### Dismissed list

Collapsed `<details>` below the suggested list, showing dismissed candidates with their reasons. "Undo" affordance flips `status='suggested'` again (useful for misclicks). No date limit in v1.

### New-competitor form pre-fill

- When `?candidate_id=N` is present, `admin_competitor_edit.html` reads `candidate.name` and pre-fills the Name input. A small banner at the top says:
  > From discovery run on {{candidate.created_at}}: *{{candidate.one_line_why}}*
- The form includes a hidden `candidate_id` input so the save handler can flip status.
- Autofill auto-fires on page load when `candidate_id` is present (saves the extra click).

## Cost & rate-limit discipline

- Expected cost per run: ~$0.05–$0.25 (Anthropic Sonnet + Tavily advanced depth for ~8 queries + a handful of fetches). Cheap enough that a weekly manual run is a rounding error.
- `MAX_TOOL_ROUNDS = 10` hard cap in the adapter.
- Candidates capped at 8 per run. If the model tries to return more, we truncate.
- **No per-run cooldown in v1.** The user decides when to run. If it becomes an issue, a 1h cooldown is a one-line add.
- **Per-run concurrency:** at most one discovery run in flight at a time across the whole install. Second-click returns a friendly "discovery already running" message.

## Auth

- Run endpoint requires admin role (same as New Competitor creation). Analysts and viewers can see candidates but can't trigger runs or add from them in v1. Revisit if analysts end up curating the watchlist.
- Dismiss endpoint: admin only (dismissals are sticky and change what future runs see).

## Testing

- Empty state: fresh install, no candidates, `ANTHROPIC_API_KEY` + `TAVILY_API_KEY` set → Discover panel renders with idle form, "Last run: Never," empty candidate list.
- Missing key: `ANTHROPIC_API_KEY` unset → key-missing nudge, Run button disabled.
- Happy path: click Run (no hint) → status card shows running → in ~30–90s, 3–8 candidate cards render. Each card has homepage + evidence chips that open in a new tab.
- Hint path: enter "focus on Australian job boards" → run → candidates skew toward AU (manual check).
- Exclusion: existing competitor "Indeed" never returned. Dismiss "Greenhouse" → run again → Greenhouse never returned.
- Adopt path: click Add on a candidate → lands on `/admin/competitors/new?candidate_id=N` with Name pre-filled and autofill stream running. Save → candidate status flips to `adopted`, `adopted_competitor_id` set. Candidate no longer appears in the suggested list.
- Dismiss path: click Dismiss → reason textarea inline → submit → card disappears with an undo affordance in the dismissed `<details>`. Undo restores.
- Concurrency: two tabs, both click Run in quick succession. Second gets the "already running" message.
- Mid-run reload: start a run, reload the page mid-flight, status card re-renders and the HTMX poll resumes.
- `/runs` shows the `discover_competitors` run with start/finish times. Live log shows the `material` event on success with candidate count.

## Acceptance criteria

1. `/admin/competitors` has a Discover panel above the Active table that renders without errors regardless of API key state, existing candidates, or in-flight runs.
2. Clicking Run enqueues a background job, creates a `Run(kind="discover_competitors")`, and navigates back to `#discover` showing the running state.
3. Discovery produces up to 8 candidates, each with a verified homepage, and never duplicates an active competitor or a previously dismissed candidate.
4. Each candidate renders with name, homepage, category chip, one-line why, and up to 5 evidence links.
5. Clicking Add navigates to `/admin/competitors/new?candidate_id=N` with the Name pre-filled; saving the form flips the candidate to `adopted` and sets `adopted_competitor_id`.
6. Clicking Dismiss marks the candidate dismissed with an optional reason; dismissed candidates are excluded from future runs.
7. Missing API keys show a key-missing nudge consistent with the Deep Research tab.
8. Discovery runs appear on `/runs` with `kind="discover_competitors"` and a `material` RunEvent on success.
9. No changes to existing Active / Inactive table behavior or to the existing New Competitor flow when `candidate_id` is absent.
10. CSS cache-buster bumped.

## What this unblocks

- **Recurring discovery.** Once the manual flow proves itself, bolt a weekly cron on `run_discover_competitors_job` (no hint) and let the live log surface "3 new candidates this week" as a notification.
- **Findings-driven discovery.** Scan `findings.summary` for named entities that aren't in `competitors` + aren't in dismissed candidates; surface them as a second source of candidates alongside the web-search loop.
- **Category-specific hints as presets.** A small row of quick-hint buttons ("AU market," "AI-first ATS," "adjacent categories") that pre-fill the hint box — lightweight, one-line UI addition.
- **Quality signal on discovery itself.** Once a few weeks of candidates exist, the `adopted` vs `dismissed` ratio per run is a quality signal on the prompt. If dismissal rate is high, tighten the prompt or constrain the category list.
