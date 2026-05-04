# Spec 06 — Predicate Review (monthly ontology hygiene)

**Status:** Draft
**Owner:** Simon
**Depends on:** [Spec 01 — Foundation](./01-foundation.md), [Spec 02 — In-card Tagging](./02-card-tagging.md), [Spec 03 — Predicate Dashboard](./03-predicate-dashboard.md), [Spec 04 — Scenario Dashboard + Chat Tools](./04-scenario-dashboard.md).
**Unblocks:** Future "predicate authoring v2" (split-state, new-predicate flows graduate from proposals into first-class authoring).

## Purpose

The Stage 1–4 surfaces all answer one question: *given the predicates we have, what does the engine currently believe and why?* That's forward-looking belief math — priors, posteriors, log-odds contributions, sensitivity bars.

There's a second question the engine never asks itself: *given the findings we've actually tagged, are these still the right predicates?* Statements drift. New product behavior emerges that doesn't map cleanly to any existing state. Two predicates start eating each other's evidence. The Bayesian math is happy to keep cranking out posteriors over a slowly-rotting ontology.

Stage 6 introduces a **monthly review pass**: an LLM-driven job that looks at each active predicate's recent confirmed evidence, decides whether the predicate is still well-formed, and surfaces its read on the per-predicate page — with a high bar for proposing structural change.

The framing on the predicate page flips. Today the "Evidence" section is a Bayesian receipt (`+1.10 Δ logit · cred 1.00 · decay`). After this spec, it's a **review pane**: the agent's qualitative read of how the findings fit, with the math demoted to a `▸ Math view` disclosure. Past tense, judgment-first.

## Non-goals (this stage)

- **No autonomous edits.** The agent never mutates a predicate. Every suggestion is a proposal that a human accepts or rejects.
- **No cross-predicate merges from the per-predicate page.** Merging p3 with p7 spans both ontologies; that decision belongs in a global proposals queue (out of scope here, sketched in §"What this unblocks").
- **No real-time review.** The job runs on a schedule. Adding a finding doesn't trigger a re-review. The on-demand button exists for analysts who want to force one.
- **No replacement of the Bayesian engine.** Posteriors keep flowing exactly as today. Review affects authoring, not math.
- **No editing UI for proposals beyond Accept / Reject / "Refine in chat."** Authoring lives in the existing chat tool (`scenarios_update_predicate`); this spec produces drafts, not a forms-based diff editor.
- **No notifications / email digest.** A row appears on `/scenarios` after the monthly run; that's it. Email is Spec 07's job.

## Design principles

1. **No change is the default.** The agent is prompted to recommend "looks good" unless thresholds are met. Most months, most predicates render a quiet card: *"Reviewed 2026-05-01, 6 findings since last review. Statement still fits, no action recommended."* Noise only when something actually moved.
2. **Wording before structure.** Suggested actions are ranked: *refine statement* → *rename state* → *reorder states* → *split state* → *new predicate* / *merge*. Cheap edits first; structural changes are escalations the agent must explicitly justify.
3. **High threshold for change.** Wording suggestion needs ≥1 finding the agent flags as "awkwardly worded against current statement." Structural suggestion needs ≥3 misfit findings OR ≥2 findings that fit a *different* predicate better. Below those bars → no suggestion.
4. **Agent proposes, human disposes.** "Move to p4" doesn't move the evidence row. It writes a `predicate_proposals` row in `pending` status. A separate batch-confirm step (this stage: simple inline Accept/Reject buttons) commits or discards.
5. **Math doesn't disappear.** The existing evidence table — Δ logit, decay, contribution — is preserved verbatim under a `▸ Math view` toggle. Anyone who wants the receipt still gets it. Default-collapsed because the review pane is what most people came for.
6. **Reviews are append-only.** Each run writes a fresh `predicate_reviews` row per active predicate. History is queryable: "what did the agent say in March?"
7. **Degrade cleanly without an LLM.** With `ANTHROPIC_API_KEY` unset, the job no-ops with a log entry. Pages still render — the review block falls back to "No review yet" and the findings list keeps its today-shape (just without fitness chips).

## Where it lives

- **Models / migrations**
  - New table `predicate_reviews` (one row per `(predicate, run)` pair).
  - New table `predicate_proposals` (proposal queue: refine_statement, rename_state, reorder_states, split_state, reassign_evidence, retire). Cross-predicate `merge_with` and `new_predicate` kinds are reserved for a future global queue spec; the schema accommodates them but no UI surfaces them this stage.
  - Add three columns to `predicate_evidence`: `fitness TEXT NULL` (`fits` | `awkward` | `misfit`), `fitness_read_as TEXT NULL`, `fitness_reviewed_at TIMESTAMP NULL`. NULL = "not yet reviewed by the monthly job."
  - Alembic migration `bb2o8p9q0r1s2_predicate_review.py`.
- **Math layer**
  - None. No new pure functions. Review is a pipeline, not math.
- **Service layer**
  - `app/scenarios/review.py` — new module. Owns the review pipeline:
    - `run_predicate_review(db, *, predicate_keys=None, run_id=None) -> ReviewRunResult` — top-level entrypoint. Iterates active predicates, calls the LLM per-predicate, writes `predicate_reviews` + updates `predicate_evidence.fitness*` + creates `predicate_proposals` rows. Tolerates partial failure: a single predicate's LLM call failing doesn't abort the run.
    - `latest_review_for(db, predicate_key) -> PredicateReviewView | None` — reads the most recent `predicate_reviews` row for the per-predicate page.
    - `pending_proposals_for(db, predicate_key) -> list[ProposalView]` — proposals still in `pending` whose `source_predicate_key` matches.
    - `digest_for_run(db, run_id) -> ReviewDigest` — counts (clean / wording / structural) for the `/scenarios` root digest row.
  - `app/scenarios/service.py` — new functions:
    - `accept_proposal(db, proposal_id, user) -> None` — applies the proposal's payload by calling existing `update_predicate` (for refine/rename/reorder/split) or by mutating `predicate_evidence.predicate_id` (for reassign). Marks the proposal `accepted`. Supersedes any older pending proposals on the same predicate of the same kind.
    - `reject_proposal(db, proposal_id, user, *, reason=None) -> None` — marks `rejected`. No mutation.
- **Job**
  - `app/jobs.py` — register `predicate_review` APScheduler job. Default cron `0 6 1 * *` (06:00 UTC on the 1st of each month). Wraps `run_predicate_review` in a `Run` row of kind `predicate_review` so progress and errors land on `/runs` like every other job. The cron string is a config knob (see §Configurability).
- **Routes** (extend `app/routes/scenarios.py`)
  - `GET /scenarios/predicates/{key}` — already exists; rendering pulls in `latest_review_for(...)` and `pending_proposals_for(...)`.
  - `POST /scenarios/proposals/{id}/accept` — analyst/admin. HTMX, returns the updated proposal card (or empty 200 on accept).
  - `POST /scenarios/proposals/{id}/reject` — analyst/admin. Body: optional `reason`.
  - `POST /api/runs/predicate-review` — admin/analyst. Enqueues a `predicate_review` Run. Mirrors the existing scenarios-recompute pattern.
  - `GET /scenarios` — root tab gains a "monthly review digest" strip when `digest_for_run` returns data for the most recent completed run.
- **Templates**
  - `app/templates/_predicate_review_block.html` — new partial. The agent's prose read + suggested-action chips at the top of the Findings section. Renders nothing visible if `latest_review_for` is None.
  - `app/templates/_predicate_findings_list.html` — new partial. The reframed findings list with fitness chips and inline reassign affordance. Replaces the body of the current Evidence section on the predicate detail page.
  - `app/templates/_predicate_math_view.html` — extracted from current `scenarios_predicate_detail.html`; contains the existing evidence table verbatim, wrapped in a `<details>` disclosure.
  - `app/templates/_predicate_proposal_card.html` — used on the predicate page (and in §"What this unblocks" by a future global queue) to render one proposal with Accept/Reject buttons.
  - `app/templates/_scenarios_review_digest.html` — the strip on `/scenarios` root summarising the latest run.
  - `app/templates/scenarios_predicate_detail.html` — refactored to compose the three new partials in the Findings section. Existing Distribution/Evolution sections untouched.
- **Static**
  - `app/static/style.css` — `.predicate-review-block`, `.predicate-review-summary`, `.predicate-review-action-chip`, `.fitness-chip` (`is-fits` / `is-awkward` / `is-misfit`), `.evidence-row-reassign`, `.proposal-card`, `.review-digest-strip`. Bump the cache buster.
- **Chat tools** (extend `app/scenarios/chat_tools.py`)
  - `scenarios_run_predicate_review(predicate_keys?)` → write tool, requires_role analyst, requires_confirmation. Triggers an on-demand review (one predicate or all).
  - `scenarios_list_pending_proposals(predicate_key?)` → read.
  - `scenarios_decide_proposal(proposal_id, decision, reason?)` → write, requires_role analyst, requires_confirmation.
  - All three thin wrappers over `run_predicate_review` / `pending_proposals_for` / `accept_proposal`+`reject_proposal`. No new business logic in the chat layer (Spec 04 rule).

## Data model

### `predicate_reviews`

```
id                       INTEGER PK
predicate_id             INTEGER FK → predicates.id
run_id                   INTEGER FK → runs.id
reviewed_at              TIMESTAMP NOT NULL
findings_seen_count      INTEGER NOT NULL
decided_no_change        BOOLEAN NOT NULL
summary_text             TEXT NOT NULL              -- the agent's prose read
suggested_actions_json   TEXT NOT NULL              -- JSON list, possibly empty
proposal_ids_json        TEXT NOT NULL              -- JSON list of created proposal ids

INDEX (predicate_id, reviewed_at DESC)
INDEX (run_id)
```

`suggested_actions_json` carries the lightweight chips shown on the page even when no proposal was created (e.g. `[{"kind":"looks_good","label":"Looks good — dismiss for 30d"}]`). When an action *did* spawn a proposal, the chip's payload includes the `proposal_id` so the UI can link straight to its card.

### `predicate_proposals`

```
id                          INTEGER PK
kind                        TEXT NOT NULL
                              -- refine_statement | rename_state | reorder_states
                              -- | split_state | reassign_evidence | retire
                              -- (reserved for future: merge_with | new_predicate)
source_predicate_key        TEXT NULL              -- predicate this is "about" (NULL for new_predicate)
target_payload_json         TEXT NOT NULL          -- proposed change, kind-specific shape
rationale                   TEXT NOT NULL
supporting_finding_ids_json TEXT NOT NULL
status                      TEXT NOT NULL          -- pending | accepted | rejected | superseded
created_at                  TIMESTAMP NOT NULL
decided_at                  TIMESTAMP NULL
decided_by                  INTEGER NULL FK → users.id
decision_reason             TEXT NULL
source_review_id            INTEGER NULL FK → predicate_reviews.id

INDEX (source_predicate_key, status)
INDEX (status, created_at DESC)
```

Payload shapes per kind:

```jsonc
// refine_statement
{ "new_statement": "..." }

// rename_state
{ "state_key": "agent", "new_label": "Agent (assisted or native)" }

// reorder_states
{ "order": ["platform", "hybrid", "agent"] }

// split_state
{
  "state_key": "agent",
  "new_states": [
    { "state_key": "agent_native", "label": "Agent-native", "prior": 0.15 },
    { "state_key": "agent_assisted", "label": "Agent-assisted", "prior": 0.10 }
  ],
  "evidence_remap": [{ "evidence_id": 4521, "to_state_key": "agent_native" }, ...]
}

// reassign_evidence
{ "evidence_id": 4380, "to_predicate_key": "p4", "to_state_key": "..." }

// retire
{ "reason_short": "absorbed into p7" }
```

`accept_proposal` reads the payload and dispatches to the existing service-layer authoring functions. No new mutation logic per kind.

### `predicate_evidence` columns

```
fitness               TEXT NULL CHECK (fitness IN ('fits','awkward','misfit'))
fitness_read_as       TEXT NULL                  -- one-line LLM gloss
fitness_reviewed_at   TIMESTAMP NULL             -- when these three columns were last touched
```

NULL on all three = "no review has touched this row yet." UI then hides the fitness chip and falls back to today's `+Δ logit` rendering for that row only.

## The job: `predicate_review`

Per-predicate flow inside `run_predicate_review`:

1. Pull the predicate (statement, states, category) and all confirmed evidence rows.
2. Build an LLM prompt with: predicate definition, every confirmed evidence row (target state, direction, strength, finding title + 1–2 sentence excerpt), the prior review's summary if one exists.
3. Ask for a structured JSON response:
   ```jsonc
   {
     "summary_text": "...",
     "decided_no_change": true,
     "fitness_per_evidence": [
       { "evidence_id": 4521, "fitness": "fits", "read_as": "...", "reassign_target_predicate_key": null }
     ],
     "suggested_actions": [
       { "kind": "refine_statement", "rationale": "...", "payload": {...}, "supporting_finding_ids": [...] }
     ]
   }
   ```
4. Apply the high-bar gates server-side, even if the LLM ignored them:
   - `refine_statement` / `rename_state` / `reorder_states` need ≥1 supporting finding.
   - `split_state` / `retire` need ≥3 misfit findings of supporting evidence.
   - `reassign_evidence` is per-finding; created 1:1 with each `fitness_per_evidence` row whose `reassign_target_predicate_key` is non-null AND target predicate exists AND is active.
   - Suggestions failing their gate are dropped before write.
5. For each surviving suggestion, create a `predicate_proposals` row.
6. Write the `predicate_reviews` row with `decided_no_change = (no proposals AND no fitness flagged ⚠/✗)`.
7. Update each `predicate_evidence` row touched in `fitness_per_evidence` with `fitness`, `fitness_read_as`, `fitness_reviewed_at = now`.
8. Supersede any prior pending proposals on this predicate of the same `kind` and same `target_payload_json` shape (avoids duplicates accumulating month-over-month).

If the LLM call fails (timeout, rate limit, malformed JSON after one retry), log + skip. Do not write a `predicate_reviews` row for that predicate; next month tries again.

After the loop, the run's row on `/runs` shows: `12 reviewed · 9 clean · 2 wording · 1 structural · 0 errors`.

## UI

### Predicate detail page — Findings section, after refactor

```
┌─ Findings under this predicate ─────────────────────────────────────────┐
│                                                                         │
│  Agent's read · reviewed 2026-05-01, 14 findings since last review      │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ Statement still fits. 8 of 14 findings cleanly map; 3 sit in      │  │
│  │ "neutral" but really concern pricing power, not distribution      │  │
│  │ control. Considered splitting "agent" — only 4 supporting cases,  │  │
│  │ below the 3-misfit bar; not yet justified.                        │  │
│  │                                                                   │  │
│  │ Suggested actions:                                                │  │
│  │   [ Refine statement ]   [ Reassign 3 findings to p4 ]            │  │
│  │   [ Looks good — dismiss for 30d ]                                │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  Filter: [ All ▾ ]  [ ◻ Awkward fits only ]    Sort: [ Recent ▾ ]      │
│                                                                         │
│  ✓ fits        2026-04-22  OpenAI Operator launch — agent-as-distrib…  │
│                Read as: supports "agent" · strong                       │
│                                                                         │
│  ⚠ awkward     2026-04-09  Indeed earnings: 'web is still our front…'  │
│                Agent's note: better fit for p4 (pricing power).         │
│                [ Move to p4 ]                                           │
│                                                                         │
│  ✓ fits        2026-03-15  ChatGPT job search browser extension…       │
│                Read as: supports "agent-assisted" (proposed)            │
│                                                                         │
│  ▸ 11 more findings                                                     │
│  ▸ Show pending + rejected (4)                                          │
│                                                                         │
│  ▸ Math view (Δ logit · decay · contribution) ← old table, unchanged    │
└─────────────────────────────────────────────────────────────────────────┘
```

Action chips dispatch:

- `[ Refine statement ]` → opens the proposal card inline (POST /scenarios/proposals/{id}/accept | reject buttons).
- `[ Reassign 3 findings to p4 ]` → expands a sub-list of the three reassign proposals, each with its own Accept/Reject.
- `[ Looks good — dismiss for 30d ]` → marks the predicate as "review-not-due-until" 30 days from now (a column on `predicates`, see §Configurability — or use the existing `decay_half_life_days` precedent and add `next_review_due_at`).
- `[ Move to p4 ]` inline → identical to the corresponding chip in the action bar; either entrypoint creates / surfaces the same proposal row.

### `/scenarios` root — monthly digest strip

Below the existing tab nav, before any tab content:

```
┌─ May review · ran 2026-05-01 06:00 ─────────────────────────────────┐
│  12 predicates reviewed · 9 clean · 2 wording suggestions ·         │
│  1 structural proposal pending · 0 errors                           │
│  [ Open p1 ]   [ Open p8 ]   [ Re-review now ]                      │
└─────────────────────────────────────────────────────────────────────┘
```

The strip auto-collapses ≥7 days after the run completes (the freshness window). The "Re-review now" button is admin/analyst only; same handler as `POST /api/runs/predicate-review`.

### Proposal card

Used inline on the predicate page wherever a chip expands. Reused unchanged by the future global queue (out of scope here).

```
┌─ Proposal · refine statement · pending ─────────────────────────────┐
│  Current: "Will buyers prefer platform-led or agent-led discovery?" │
│  Proposed: "Will primary discovery happen on a platform UI or       │
│             through an agent (assistant-mediated) interface?"       │
│                                                                     │
│  Why: 4 of the 14 findings since the last review use "agent" to     │
│  mean "assistant", not "autonomous browser". The current statement  │
│  doesn't disambiguate.                                              │
│                                                                     │
│  Supporting findings: #4521  #4380  #4112  #4067                    │
│                                                                     │
│  [ Accept ]  [ Reject ]  [ Refine in chat ]                         │
└─────────────────────────────────────────────────────────────────────┘
```

`Refine in chat` deep-links to the chat session with a pre-filled prompt referencing the proposal id; from there the existing `scenarios_update_predicate` chat tool does the work.

## Routes

```
GET  /scenarios                                       → digest strip if recent run
GET  /scenarios/predicates/{key}                      → review block + reframed findings + math toggle
POST /scenarios/proposals/{id}/accept                 → analyst/admin
POST /scenarios/proposals/{id}/reject                 → analyst/admin
POST /api/runs/predicate-review                       → admin/analyst, enqueues Run
```

All HTMX-friendly: accept/reject return the swapped card partial.

## Configurability

New rows in the existing scenarios config table (or `app_settings` — match the pattern Spec 01 used):

| Key                                    | Default      | Meaning                                               |
|----------------------------------------|--------------|-------------------------------------------------------|
| `predicate_review_cron`                | `0 6 1 * *`  | APScheduler cron expression                           |
| `predicate_review_misfit_threshold`    | `3`          | Min misfit findings to allow a structural proposal    |
| `predicate_review_better_fit_threshold`| `2`          | Min better-fit-elsewhere findings for reassign batch  |
| `predicate_review_dismiss_days`        | `30`         | Cooldown after "looks good — dismiss"                 |
| `predicate_review_max_evidence_in_prompt` | `40`      | Cap on evidence rows passed to LLM (sample if more)   |

Optional new column `predicates.next_review_due_at TIMESTAMP NULL` — set when "Looks good — dismiss for 30d" is clicked, honored by `run_predicate_review` to skip the predicate until the date passes. Avoids needing a separate review-cooldown table.

## Known limitations

1. **One LLM call per predicate.** With 12–20 active predicates, a monthly run is 12–20 calls. Acceptable. If the predicate set grows to >50, batch into one call per category.
2. **Evidence cap blunts review for hot predicates.** If `findings_seen_count > 40`, the prompt samples. The agent flags this in its summary ("reviewed 40 of 87"). Full-fidelity review at scale is a future spec.
3. **Reassign across predicates is one-at-a-time.** The action chip groups proposals visually, but each is a separate row in `predicate_proposals`. Bulk-accept for a group would be a small UI win — not in this spec.
4. **Superseding logic is shape-equality.** Two reviews proposing the same `refine_statement` payload string supersede the older one. If the LLM rewords the same idea two months in a row, both stay live until one is decided. Acceptable.
5. **No "undo" on accept.** Accepted proposals mutate the predicate via existing service functions; the audit trail is the proposal row + the user's edit history. Reverting is a manual edit.
6. **First-run fitness columns.** Until the first monthly run touches a predicate's evidence, every row's `fitness` is NULL and the findings list renders today's plain layout. This is fine and expected; the Math view always shows the receipts regardless.

## Testing

`scripts/verify_predicate_review.py` — new script paralleling `verify_scenarios_math.py`. Uses an LLM stub that returns canned JSON.

- **Threshold gate: refine_statement**
  - Stub returns one `refine_statement` action with 1 supporting finding → proposal created.
  - Stub returns one `refine_statement` action with 0 supporting findings → dropped, no proposal.
- **Threshold gate: split_state**
  - Stub returns `split_state` with 2 misfit findings → dropped.
  - Stub returns `split_state` with 3 misfit findings → proposal created.
- **`decided_no_change`**
  - Stub returns no actions and all `fitness="fits"` → review row has `decided_no_change = true`.
  - Stub returns one `awkward` fitness with no action → `decided_no_change = false` (because there's at least one ⚠).
- **Supersede same-shape**
  - Run twice with identical canned `refine_statement` payload → only one pending row, the older marked `superseded`.
- **Apply: refine_statement**
  - Accept the proposal → predicate's `statement` field equals the payload's `new_statement`.
- **Apply: reassign_evidence**
  - Accept → the `predicate_evidence` row's `predicate_id` is updated; the original predicate's `predicate_detail` no longer lists it; the target predicate's does.
- **Cooldown**
  - Set `predicates.next_review_due_at = now + 30d` → `run_predicate_review` skips that predicate, no row written.
- **LLM unset**
  - With `ANTHROPIC_API_KEY` unset, `run_predicate_review` returns a `ReviewRunResult` with `errors=[]`, `reviewed=0`, no rows written. Predicate page still loads.

Stage-1/2/3/4 verify scripts continue to pass.

## Acceptance criteria

1. Migration applies cleanly on a Stage-4 DB; rollback drops the new tables and columns without touching data.
2. APScheduler registers the `predicate_review` job with the configured cron; appears on `/runs` after the first scheduled fire.
3. With `ANTHROPIC_API_KEY` unset, the job logs and exits without writing rows; existing pages render unchanged.
4. Per-predicate page shows the review block above the findings list when a `predicate_reviews` row exists; renders nothing visible when none.
5. When the latest review has `decided_no_change = true` and no flagged fitness, the block shows the summary line only — no suggestion chips.
6. When proposals exist, each chip expands to a `_predicate_proposal_card.html` partial with Accept / Reject / Refine-in-chat.
7. Accept on a `refine_statement` proposal updates `predicates.statement`; reject leaves it untouched and marks the proposal `rejected`.
8. Inline `[ Move to p4 ]` on an awkward finding row creates the same `reassign_evidence` proposal as the bulk chip in the action bar (i.e., dedupe; no two proposals from one finding fitness row).
9. Math view toggle reveals the existing evidence table with no visual or data regressions vs. Stage 3.
10. `/scenarios` root shows the digest strip when the most recent `predicate_review` run completed within the last 7 days; collapses or hides afterward.
11. On-demand `POST /api/runs/predicate-review` enqueues a Run of kind `predicate_review` and the same per-predicate flow runs.
12. CSS cache buster bumped; no regressions to `/scenarios` Predicates / Scenarios / Evidence / Settings tabs.
13. Chat tools `scenarios_run_predicate_review`, `scenarios_list_pending_proposals`, `scenarios_decide_proposal` are registered, schema-validated, and obey the role gates.
14. Stage-1/2/3/4 verify scripts + the new `verify_predicate_review.py` all green.

## What this unblocks

- **Global proposals queue.** Once `predicate_proposals` exists with `merge_with` and `new_predicate` reserved in the schema, a future spec adds a `/scenarios?tab=proposals` view that surfaces cross-predicate proposals (the agent flagging that p3 and p7 overlap, or that an unmappable cluster of 5 findings deserves a brand-new predicate). The card partial and the accept/reject endpoints already work.
- **Belief-shift digest (Spec 07 candidate).** A monthly email that combines this stage's review digest with a "predicates that moved >5pp" report. All data already in the DB after this stage lands.
- **Predicate authoring v2.** With the proposal pipeline in place, the chat tool can graduate from "edit one field" to "draft a new predicate end-to-end and submit it as a `new_predicate` proposal." The current scenarios_update_predicate tool stays focused on edits; a sibling `scenarios_propose_new_predicate` would write directly into the queue.
