# Spec 02 — In-card Predicate Tagging (LLM pre-fill, manual confirm)

**Status:** Draft
**Owner:** Simon
**Depends on:** [Spec 01 — Foundation](./01-foundation.md). Reuses every Stage-1 table; the only schema change is one nullable column on `findings`.
**Unblocks:** Spec 03 (predicate dashboard — needs evidence flowing in to be worth building), Spec 04 (scenario dashboard), Spec 05 (sensitivity + assumption controls). Also retires the half-built "ask follow-up question" affordance on the stream card.

## Purpose

Stage 1 stood up the math + storage. Today the only way evidence reaches the engine is `python -m scripts.add_evidence` — fine for smoke tests, useless at the volume the scanner produces (tens of findings per scan, hundreds per week).

Stage 2 closes the loop. **The same stream where you already triage findings becomes the evidence-entry surface.** Every finding gets a small predicate-tag affordance on its card. A cheap LLM (Haiku 4.5) pre-fills the proposed tag the moment a scan completes, so by the time you open `/stream` the cards already say "→ p1 agent (proposed)". Flip the card → confirm or edit → posterior recomputes immediately and the tag turns green.

This replaces the existing flip-to-ask-follow-up-question UI on the same card. That feature was half-built and never delivered the value the predicate workflow will. Same gesture, more useful payload.

The output of Stage 2 is **a steady stream of confirmed evidence flowing into the engine, with the operator spending seconds-per-finding rather than minutes-per-finding to keep beliefs current.**

## Non-goals

- **No separate queue page.** The stream is the queue. A "show only untagged findings" filter exists but isn't a new top-level view.
- **No batch confirm-all UI.** Each evidence row is confirmed individually. Speed comes from pre-fill quality, not bulk operations.
- **No predicate / scenario dashboard.** Stage 3.
- **No scenario probability widget on `/stream`.** The cards stay focused on triage; Stage 4 is where the engine output is surfaced.
- **No retraining loop / classifier eval.** Stage 5+. We collect ground truth (confirms / edits / rejects) by stamping `classified_by` and the proposed-vs-final delta; the analysis pass is later.
- **No ordinal-aware classifier hints** (e.g. "support for `optional` is also weak contradict for `required_centralized`"). The LLM proposes one target state; multi-state nuance is left to the operator's edit.
- **Don't remove the `SignalView.question` column.** The flip-to-question UI is gone, but the column stays so historical rows on the competitor profile keep rendering. A separate cleanup PR can drop it once those rows are gone.
- **No mobile-specific gesture.** Reuse the existing `flip-toggle` button. Mobile swipe support comes later if needed.

## Design principles

1. **Pre-fill, don't gate.** The LLM proposes; the human commits. No evidence flows into the engine without a `confirmed_at` stamp. This keeps the math clean from the start — bad classifier behavior degrades the queue, not the beliefs.
2. **Cheap model, cached prompt.** Haiku 4.5 with the predicate roster in the cached system block. Per-finding cost is ~$0.001 with cache hits. A daily budget cap (env var) hard-stops the sweep if abused.
3. **In-card UX.** Tagging belongs where you're already reading findings. No context switch, no separate tab.
4. **Multi-evidence per finding.** Soft-cap of 2 (per PRD) enforced in the form; route layer accepts more so a future change of mind is one config flip.
5. **Soft-reject.** A "no, this finding doesn't bear on a predicate" is a useful training signal. Stamp `classified_by="user_rejected"`, leave the row, exclude from recompute.
6. **Live recompute.** Confirm one evidence → recompute that one predicate immediately (~tens of ms). The badge updates in place. Satisfying, fast, no batched lag.
7. **Idempotent classifier sweep.** Each finding gets classified once. `findings.scenarios_classified_at` records "the LLM has looked at this" so the sweep is safe to re-run.
8. **Failure-soft.** Missing `ANTHROPIC_API_KEY` → sweep skips silently, cards render with no proposal but a manual "+ Tag" affordance. Same shape as the rest of the app.

## Where it lives

- **Migration**
  - `alembic/versions/<new>_findings_scenarios_classified_at.py` — adds one nullable `DateTime` column to `findings`. Single-column add, safe in batch_alter_table.
- **Models**
  - `app/models.py::Finding` — append `scenarios_classified_at: Mapped[datetime | None]` field.
- **Classifier**
  - `app/scenarios/classifier.py` — `classify_finding(finding, predicate_roster) -> list[ProposedEvidence]`. One Haiku call per finding. System prompt cached. JSON output via prompt instruction (not tool use; matches the `llm_classify.py` pattern in this repo).
  - `ProposedEvidence` is a NamedTuple: `(predicate_key, target_state_key, direction, strength_bucket, confidence, reasoning)`.
- **Sweep worker**
  - `app/scenarios/sweep.py::classify_unclassified(db, *, limit=200, since=None) -> SweepResult` — picks N findings with `scenarios_classified_at IS NULL`, calls the classifier on each, writes evidence rows with `classified_by="llm"` and `confirmed_at=NULL`, stamps `scenarios_classified_at`. Respects the daily budget cap. Returns counts: `{findings_processed, evidence_proposed, skipped_no_signal, skipped_budget}`.
  - `SweepResult` is a NamedTuple — same convention as the VoC sweep result in `app/voc_themes.py`.
- **Job hook**
  - `app/jobs.py::run_scenarios_classify_sweep_job(triggered_by="scheduled", limit=None, run_id=None)` — wraps `sweep.classify_unclassified` in a `Run(kind="scenarios_classify_sweep")`. Mirrors `run_scenarios_recompute_job` shape.
  - `app/jobs.py::run_scan_job` — at the end, after the existing recompute hooks, enqueue a `scenarios_classify_sweep` run with `limit=` set to the count of new findings. Async, fire-and-forget; if it fails the scan still completes successfully.
- **Routes** (new file)
  - `app/routes/scenarios.py` — registered alongside the others in `app/main.py`.
  - `GET /partials/finding/{finding_id}/predicate_form` — HTMX partial: renders the back-of-card tagging form pre-filled with current evidence + the LLM proposal. Returns `_predicate_form.html`.
  - `POST /partials/finding/{finding_id}/evidence` — create one evidence row (form fields: `predicate_key`, `target_state_key`, `direction`, `strength_bucket`, optional `notes`). Stamps `confirmed_at`, `classified_by="user_override"` if it differs from an LLM proposal else `manual`. Triggers `recompute_predicate` synchronously. Returns the re-rendered card via `_stream_card.html`.
  - `POST /partials/finding/{finding_id}/evidence/{evidence_id}/confirm` — confirm an existing LLM-proposed row as-is. Stamps `confirmed_at`, leaves `classified_by="llm"`. Triggers recompute, returns re-rendered card.
  - `POST /partials/finding/{finding_id}/evidence/{evidence_id}/reject` — soft-reject. Stamps `classified_by="user_rejected"`, leaves `confirmed_at=NULL`. Triggers recompute (in case the row was previously confirmed and we're now reversing — defensive). Returns re-rendered card.
  - `POST /partials/finding/{finding_id}/evidence/{evidence_id}/edit` — edit-in-place: fields same as create. Stamps `classified_by="user_override"`, sets `confirmed_at=now`. Triggers recompute. Returns re-rendered card.
  - All routes auth-gated to admin/analyst (matches the existing `/stream` actions).
- **Templates**
  - `app/templates/_stream_card.html` — gut the textarea/Pin&Ask back-of-card; replace with a `<div class="signal-card-back">` that HTMX-loads the predicate form on first flip.
  - `app/templates/_predicate_form.html` — new partial. Lists existing evidence rows (each with confirm/edit/reject buttons), then a "+ Add" affordance up to the soft cap of 2. Predicate dropdown, dependent state dropdown (changes when predicate changes), direction radio, strength radio, optional notes.
  - `app/templates/_predicate_badges.html` — new partial. Renders the chip(s) on the card front: untagged / proposed (amber) / confirmed (green) / rejected (greyed). Embedded into `_stream_card.html` via `{% include %}`.
  - `app/templates/stream.html` — remove the inline JS that pulls / pushes the question text. Add inline JS for the new flip flow (HTMX-load form on flip, swap card on form submit).
- **Static**
  - `app/static/style.css` — add `.predicate-tag-chip` (proposed / confirmed / rejected variants), tweak `.signal-card-back` to accommodate the form. Bump cache-buster.
- **Removed surface**
  - `app/ui.py::POST /partials/finding/{finding_id}/question` — delete the route. The "Pin & ask" code path goes away. The plain-pin path stays (clicking "Pin" on the front of the card still works via the existing stream_view path).
  - The textarea + "Pin & ask" button in `_stream_card.html` — gone.
  - Question rendering on the competitor profile (lines 109–115 of `_stream_card.html`) — leave alone. Existing `view.question` rows still render; we just don't write new ones.
- **Config**
  - `app/env_keys.py` — register `SCENARIOS_CLASSIFIER_DAILY_BUDGET_USD` (optional float, default 1.00) and `SCENARIOS_CLASSIFIER_MODEL` (optional string, default `claude-haiku-4-5-20251001`).
- **Verify script extension**
  - `scripts/verify_scenarios_math.py` — extend to cover: classifier output shape validation against a stub Anthropic client; sweep idempotency (`scenarios_classified_at` set after sweep, second sweep is a no-op); reject flow excludes evidence from recompute.
- **Docs**
  - This file.

Stage 1 file structure stays untouched.

## Data model

One column added to an existing table. No new tables.

```python
# app/models.py::Finding (appended near the bottom of the existing class)

# Stamped by app/scenarios/sweep.py once the LLM classifier has looked
# at this finding (whether or not it produced any evidence rows). NULL
# = not yet classified; the sweep picks these up. Lets us re-run the
# sweep cheaply without re-querying every finding the LLM has already
# considered.
scenarios_classified_at: Mapped[datetime | None] = mapped_column(
    DateTime, nullable=True, index=True,
)
```

```python
# alembic/versions/<new>_findings_scenarios_classified_at.py

def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(
            sa.Column("scenarios_classified_at", sa.DateTime(), nullable=True),
        )
    op.create_index(
        "ix_findings_scenarios_classified_at",
        "findings",
        ["scenarios_classified_at"],
    )
```

Existing tables in use:
- `predicate_evidence` — same shape as Stage 1. Distinguishing `classified_by` values:
  - `manual` — CLI-entered, pre-confirmed at insert.
  - `llm` — LLM proposal awaiting human confirm. `confirmed_at IS NULL`.
  - `user_override` — LLM proposal that the operator edited. `confirmed_at IS NOT NULL`.
  - `user_rejected` — LLM proposal the operator said no to. `confirmed_at IS NULL` (recompute filter excludes it).
- `predicates` / `predicate_states` — read by the classifier prompt and the form dropdowns.
- `predicate_posterior_snapshots` — written by `recompute_predicate` on each confirm/edit/reject.

## Classifier

`app/scenarios/classifier.py`. One Haiku call per finding. System prompt holds the predicate roster and instruction; user message holds the finding. System block is cache-controlled — for a 100-finding scan, the predicate roster is sent once and cached; remaining 99 calls hit the cache.

### Prompt skeleton

```
SYSTEM (cached):

You are mapping competitive-intelligence findings to predicates in a market belief
model for {{our_company}} in {{our_industry}}.

For each finding you receive, return a JSON object with one field: `evidence`.
`evidence` is an array of 0–2 objects. 0 = the finding doesn't bear on any
predicate. 1 = the finding meaningfully moves one predicate. 2 = the finding
moves two predicates (rare; only when both are clearly affected by the same
underlying observation, not when one is a downstream consequence).

Each evidence object has:
- predicate_key: one of {{predicate_keys}}
- target_state_key: must be a valid state for that predicate (see roster below)
- direction: "support" | "contradict" | "neutral"
- strength_bucket: "weak" | "moderate" | "strong"
- confidence: 0.0–1.0 — how confident you are in this mapping
- reasoning: one short sentence explaining the link

Strength bucket guide:
- strong: a definitive, public, executed move (a launch, an acquisition, an
  earnings call statement) that directly demonstrates the state.
- moderate: a credible signal short of execution (named hire suggesting a
  direction, partnership announcement, public roadmap commitment).
- weak: an indirect signal (job posting, blog post, passing exec mention).

Predicate roster:
{{for each predicate}}
  {{predicate.key}} ({{predicate.category}}): {{predicate.statement}}
    States: {{state_key}}={{state_label}} | {{state_key}}={{state_label}} | …
{{end}}

Return ONLY the JSON object. No code fences. No commentary.

USER (per-finding):

Competitor: {{finding.competitor}}
Source: {{finding.source}}  Signal type: {{finding.signal_type}}
Published: {{finding.published_at or finding.created_at}}
Title: {{finding.title}}

Content:
{{finding.summary or finding.content[:2000]}}
```

Output parsing reuses the JSON-extractor pattern from `llm_classify.py`:
- Strip code fences if any.
- Pull the first `{...}` block.
- Validate every object's `predicate_key` exists in the roster, `target_state_key` is a valid state for that predicate, `direction ∈ {support, contradict, neutral}`, `strength_bucket ∈ {weak, moderate, strong}`, `confidence ∈ [0, 1]`. Skip invalid entries.
- Cap the array at 2 (truncate the rest with a debug log if the model overshoots).

### Cost tracking

Each call writes a `UsageEvent` row (existing infrastructure) with `provider="anthropic"`, `operation="messages.create"`, model and token counts, computed `cost_usd`. Same as `llm_classify.py`. The sweep checks the daily total before each call:

```python
spent_today = db.query(func.sum(UsageEvent.cost_usd)).filter(
    UsageEvent.provider == "anthropic",
    UsageEvent.extra["scenarios_classifier"].as_boolean() == True,  # marker
    UsageEvent.ts >= today_utc_midnight,
).scalar() or 0.0
if spent_today >= SCENARIOS_CLASSIFIER_DAILY_BUDGET_USD:
    return SweepResult(..., skipped_budget=remaining_count)
```

Exact `extra` shape will mirror what `usage.py` emits today. Goal: cheap, auditable, capped.

## Worker / job flow

### Sweep

`app/scenarios/sweep.py::classify_unclassified(db, *, limit=200, since=None)`:

1. Query findings with `scenarios_classified_at IS NULL`, ordered by `created_at DESC`, limited to `limit`. Optional `since` filter so a manual run can scope to "the last week".
2. Load the predicate roster once (predicates + states).
3. For each finding:
   - Check daily budget. If exhausted, set `skipped_budget` count and break.
   - Call `classifier.classify_finding(finding, roster)`. On failure (network, JSON parse, validation): stamp `scenarios_classified_at = now` (so we don't retry forever) but write zero evidence. Increment `skipped_no_signal`.
   - On success with N evidence rows: insert N `predicate_evidence` rows with `classified_by="llm"`, `confirmed_at=NULL`, `notes=reasoning`, `credibility=` source default for the finding's source, `observed_at=` finding's `published_at` or `created_at`.
   - Stamp `scenarios_classified_at = now` on the finding.
4. Commit per-finding (so a partial sweep failure leaves a clean state — already-classified findings stay classified).
5. Return `SweepResult(findings_processed, evidence_proposed, skipped_no_signal, skipped_budget)`.

### Job

`app/jobs.py::run_scenarios_classify_sweep_job(triggered_by, limit=None, run_id=None)`:

- Same `_start_run` / `_finish_run` shape as the recompute job.
- Logs `level="info"` with the SweepResult counts.
- Logs `level="warn"` if `skipped_budget > 0`.
- Mark Run `error` on uncaught exceptions; partial successes still commit.

### Hook into scan completion

`run_scan_job` — at the end, just before `_finish_run`, enqueue a `scenarios_classify_sweep` run with `job_args={"limit": <new findings count>}`. Use the existing `enqueue_run()` helper. The scan's success doesn't depend on the sweep; if the queue is full or the dispatcher is offline, the sweep is dropped and the next manual or scheduled run picks up the unclassified findings.

### Manual trigger

`POST /api/runs/scenarios-classify-sweep` (admin/analyst). Useful for re-running after a predicate set change, or for back-filling historical findings.

## UI

### Card front: predicate tag chip

Right after the existing badges in `_stream_card.html`'s `<header class="signal-card-head">`, render:

```jinja
{% include "_predicate_badges.html" %}
```

`_predicate_badges.html` reads from `f._predicate_evidence` (loaded in the route as a per-finding list, so we don't N+1):

- 0 evidence rows total → no chip. (The "+ Tag" affordance lives on the back, surfaced via the existing `flip-toggle` button.)
- ≥1 evidence rows where `confirmed_at IS NOT NULL` and `classified_by != "user_rejected"` → green chip(s). Format: `→ p1 agent ✓`. Multiple confirmed = stacked chips up to 2, then `+N more`.
- All evidence rows are `classified_by="llm"` and `confirmed_at IS NULL` → amber chip: `→ p1 agent (proposed)`.
- All evidence rows `classified_by="user_rejected"` → muted grey: `tag rejected`.

Chip click = same as flip-toggle: opens the back of the card to the form.

### Card back: predicate form

`_predicate_form.html` replaces the textarea and Pin/Cancel buttons in the existing `.signal-card-back` div.

Layout:
```
┌─ Predicate evidence for this finding ──────────────┐
│ [existing evidence row 1]                          │
│   p1 agent · support / strong · "OpenAI launch..."│
│   [Confirm] [Edit] [Reject]                        │
│                                                    │
│ + Add another (1 of 2)                             │
│                                                    │
│ [Done] [Cancel]                                    │
└────────────────────────────────────────────────────┘
```

When user clicks **+ Add** (or there are zero existing rows on first open), a sub-form expands inline:

```
Predicate:    [▾ p1: Distribution control          ]
Target state: [▾ agent: Agent-mediated             ]    (filtered by predicate)
Direction:    ( ) support  ( ) contradict  ( ) neutral
Strength:     ( ) weak  ( ) moderate  ( ) strong
Notes:        [                              ]
              [Save] [Cancel]
```

The state dropdown is dynamic — when predicate changes, swap the options via HTMX `hx-get="/partials/predicates/{key}/states"` (a tiny JSON / HTML endpoint that returns the `<option>` list). Avoids dumping every state for every predicate into the page on first render.

If the LLM proposed a row, that row is rendered with **Confirm** as the primary button (one click → posterior moves) and an **Edit** button to tweak the proposal first.

### Untagged-findings filter

Add an option to the existing stream filter: `?tagged=untagged` (and `tagged=any`, the default). `untagged` shows findings where every evidence row is rejected OR there are no evidence rows AND `scenarios_classified_at IS NULL`. Useful for "let me sweep the unclassifieds the LLM missed".

### "Pin" affordance

Stays on the front of the card. The plain pin (no question) is unaffected — it goes straight to `stream_view` like before. The "Pin & ask" affordance is gone with the question form. If you want to pin and tag in the same gesture, two clicks: pin button on front, then flip and tag.

## Configurability

| Knob | Where | Default |
|---|---|---|
| Classifier model | env `SCENARIOS_CLASSIFIER_MODEL` | `claude-haiku-4-5-20251001` |
| Daily $ cap on classifier spend | env `SCENARIOS_CLASSIFIER_DAILY_BUDGET_USD` | `1.00` |
| Soft cap on evidence per finding | constant `MAX_EVIDENCE_PER_FINDING` in `app/scenarios/sweep.py` (and form template) | `2` |
| Sweep batch size | `limit` arg on the job | `200` |
| Auto-sweep on scan completion | always on (drop the `enqueue_run` call to disable) | on |

The likelihood / decay / credibility config from Stage 1 stays exactly as is — the LLM proposes the *evidence shape*, not the math constants.

## Known limitations

1. **Strength calibration.** Haiku has no priors about *your* market — its idea of "strong" support may differ from yours. Mitigation: the operator's edits over the first ~50 findings give us a calibration delta that informs Stage 5's prompt-tuning pass.
2. **No evidence dedup across findings.** If two findings report the same news (e.g. an Indeed press release and a Crunchbase echo of it), we get two evidence rows. Both contribute. Stage 5 may add a `dedup_key` based on URL or content hash.
3. **No retry on transient failure.** A rate-limit or timeout sets `scenarios_classified_at` and writes 0 evidence — the finding never gets reclassified. Acceptable for v1; add a `scenarios_classifier_attempts` counter in Stage 5 if it becomes a real loss source.
4. **Predicate roster fits in one prompt.** Today: 8 predicates × ~120 chars = ~1KB. Cached. If the roster grows past 20 predicates, the cached prompt gets large; revisit chunking.
5. **No per-predicate confidence weighting.** The LLM returns a `confidence` field but it's stored only in `notes` for now. Stage 5 may multiply credibility by confidence.

## Testing

`scripts/verify_scenarios_math.py` extended:

- **Classifier output validation** (no real API call): inject a stubbed Anthropic client that returns canned JSON. Verify that `classifier.classify_finding`:
  - Parses valid JSON correctly.
  - Skips entries with unknown predicate keys.
  - Skips entries where the target state isn't valid for the predicate.
  - Caps at 2 entries when 3 are returned.
  - Returns empty list on JSON parse failure.

- **Sweep idempotency**: insert 5 findings + the seed. Run sweep with stub classifier returning one valid evidence per finding. Verify:
  - 5 evidence rows written, all `classified_by="llm"`, all `confirmed_at IS NULL`.
  - 5 findings have `scenarios_classified_at` stamped.
  - Re-running the sweep is a no-op (zero new evidence).
  - Stamping `scenarios_classified_at = NULL` on one finding and re-running picks up exactly that one.

- **Confirm path**: insert one LLM-proposed row, call the confirm route logic directly. Verify `confirmed_at` set, `classified_by` unchanged, `recompute_predicate` ran (snapshot row delta).

- **Edit path**: same with new field values. Verify row updated, `classified_by="user_override"`, `confirmed_at` set, recompute ran.

- **Reject path**: `classified_by="user_rejected"`, `confirmed_at` stays NULL. Recompute ran. Posterior excludes the row (i.e. P returns to prior if this was the only evidence on the predicate).

- **Multi-evidence**: classifier proposes 2 evidence on different predicates → both written, both predicates get separate snapshots after confirm.

- **Budget guard**: stub classifier with predictable cost; set `SCENARIOS_CLASSIFIER_DAILY_BUDGET_USD=0.001` (one call's worth); run sweep against 3 findings. Verify only 1 classified, `skipped_budget=2`.

## Acceptance criteria

1. Migration applies cleanly forward and backward against a copy of prod.
2. `app/models.py::Finding.scenarios_classified_at` populated after the sweep runs against a finding (whether or not evidence was produced).
3. Triggering `run_scan_job` ends with a queued `scenarios_classify_sweep` Run; sweep produces evidence rows for new findings.
4. With `ANTHROPIC_API_KEY` unset, sweep returns silently with `skipped_no_signal=N`; cards render with the manual "+ Tag" affordance and no chip.
5. `/stream` shows amber `(proposed)` chips on cards with LLM evidence.
6. Flipping a card opens the predicate form populated with existing evidence + proposals. State dropdown updates when predicate changes.
7. Clicking **Confirm** on an LLM proposal: chip turns green, `predicate_evidence.confirmed_at` set, `predicate_posterior_snapshot` row added, `predicate_states.current_probability` updated.
8. Clicking **Edit** with changed fields: row updated with `classified_by="user_override"`, recompute ran, chip green.
9. Clicking **Reject**: row stamped `user_rejected`, posterior unchanged from prior (or returns to prior if this was the only evidence).
10. Adding a second evidence on the same finding works; "+ Add" disappears at 2 rows.
11. The "Pin & ask" textarea + endpoint (`POST /partials/finding/{id}/question`) are gone; plain "Pin" still works via the front-of-card pin button.
12. `scenarios_classifier_daily_budget_usd` enforced — exceeding it writes a `level="warn"` RunEvent and stops the sweep.
13. `?tagged=untagged` filter on `/stream` shows only findings with no confirmed evidence.
14. `verify_scenarios_math.py` still green; new sections all pass.
15. The Stage-1 CLI (`scripts/add_evidence.py`) and recompute job continue to work unchanged.

## What this unblocks

- **Spec 03 — Predicate dashboard.** With evidence flowing in, the predicate-card view + sparklines + most-shifting list become worth building. A dashboard with no data is empty calories.
- **Spec 04 — Scenario dashboard + sensitivity.** The live scenario probability widget needs a steady stream of confirmed evidence to be meaningful. Stage 2 is what makes it meaningful.
- **Spec 05 — Assumption controls + governance.** Confirms / edits / rejects are the ground truth Stage 5 uses to suggest prompt tweaks, surface high-disagreement predicates, and flag calibration drift.
- **Stage-2.5 enhancement (small)**: per-finding "explain" tooltip showing the LLM's `reasoning` string and `confidence` so the operator can see why the proposal was made.
