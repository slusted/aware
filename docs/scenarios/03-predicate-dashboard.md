# Spec 03 — Predicate Dashboard (read-only)

**Status:** Draft
**Owner:** Simon
**Depends on:** [Spec 01 — Foundation](./01-foundation.md) (data + math + snapshots), [Spec 02 — In-card Tagging](./02-card-tagging.md) (evidence flowing in).
**Unblocks:** Spec 04 (scenario probability widget — needs the visual language already established here), Spec 05 (assumption controls — turns the read-only knobs into editable forms).

## Purpose

Stage 1 built the math, Stage 2 built the evidence-entry UX. Today the engine is running silently — you can confirm proposals on `/stream` and watch the chip turn green, but there's nowhere to look at *what the engine has concluded*.

Stage 3 fills that gap. A new `/scenarios` route, default tab is **Predicates**, default view is a sortable grid of cards — one per active predicate — showing current state probabilities, a sparkline over the last 90 days, evidence count, and a 30-day velocity pill. Clicking a card expands inline to the per-state sparklines + the evidence drill-down (each row showing the actual log-odds contribution from that finding, so movement is fully attributable).

This is the **first user-facing surface that exposes the engine's output.** Read-only; tuning lives in Stage 5. The point of this stage is *trust the math by being able to see it*.

## Non-goals (this stage)

- **No editing.** Predicate priors, scenario weights, likelihoods, source credibilities — all still SQL. Stage 5 introduces forms.
- **No scenario probability widget.** Stage 4. Tabs reserve the slot but it 404s in this PR.
- **No predicate authoring UI.** Predicates are still seeded from JSON via `scripts/seed_scenarios.py`. Adding/removing a predicate is a deliberate, infrequent operation; Stage 5 surfaces it.
- **No alerting / digest emails on belief shifts.** Stage 6.
- **No interactive charts.** Sparklines are pre-rendered inline SVG (no JS chart library, no hover-to-inspect tooltips this stage). The data is in `predicate_posterior_snapshots` if a future spec wants a richer view.
- **No history-replay slider.** "What did beliefs look like 3 weeks ago" is a question for later, not v1.
- **No mobile-specific layout.** Desktop-first; the cards stack on narrow viewports via the existing `.dashboard-grid` class.

## Design principles

1. **Read, then trust, then tune.** Stage 3 only lets you read. Editing in Stage 5 inherits all the visual conventions established here, so the operator's mental model is set before the controls appear.
2. **One card per predicate.** Density vs. clarity tradeoff lands on the clarity side: 8–20 predicates fit comfortably as cards; tables would be denser but fight the "current probabilities" visualization.
3. **Inline expand, not separate page.** Clicking a card reveals the drill-down in place. No navigation = no context loss = faster scanning of the predicate set.
4. **Movement is attributable.** Each evidence row in the drill-down shows the log-odds delta it contributed. "Why did p1 move?" is answered without reading source code.
5. **Independence assumption surfaced once.** Header pill on the page, not on every card. Reminder, not noise.
6. **No new math the engine doesn't already use.** Velocity, entropy, and per-row log-odds are derived from the existing snapshot + evidence tables and the existing `posterior.py` formulas. No new persistence.

## Where it lives

- **Models / migrations**
  - None. Reads from `predicates`, `predicate_states`, `predicate_evidence`, `predicate_posterior_snapshots`, `findings`. No schema change.
- **Math layer (pure, no DB)**
  - `app/scenarios/posterior.py` — extended with three new pure functions:
    - `shannon_entropy(probabilities: dict[str, float]) -> float` — normalized to `[0, 1]` (i.e. divided by `log(N)` so a 2-state and a 3-state predicate are comparable).
    - `velocity_pp(prior_p: float, current_p: float) -> float` — percentage-point delta. Trivial wrapper, lives here for symmetry.
    - `log_odds_contribution(evidence: EvidenceInput, likelihood_table, half_life_days, now) -> float` — the per-row `log(LR) * credibility * decay` that `compute_posterior` accumulates internally; surfaced for the evidence drill-down.
- **Service layer**
  - `app/scenarios/dashboard.py` — new module. DB-aware queries that produce the dict shapes the templates render. Functions:
    - `predicate_summary(db) -> list[PredicateSummary]` — one entry per active predicate with current_probabilities, last_updated, evidence_count, velocity_pp_30d, entropy, sparkline_points. Powers the main grid.
    - `predicate_detail(db, predicate_key) -> PredicateDetail` — per-state sparklines + evidence drill-down with log-odds contributions. Powers the inline expand.
    - `evidence_list(db, *, limit=200, offset=0) -> list[EvidenceListEntry]` — flat confirmed-evidence list for the Evidence tab.
- **Routes** (extend `app/routes/scenarios.py`)
  - `GET /scenarios` — main page. Query param `?tab=predicates|evidence` (default `predicates`).
  - `GET /scenarios/predicates/{predicate_key}/expand` — HTMX partial returning the inline expand panel for one predicate. Targets `#predicate-card-{key}` on the page.
  - `GET /scenarios/evidence` — alias for `/scenarios?tab=evidence` so the evidence tab can be deep-linked.
  - `POST /api/runs/scenarios-recompute` — manual trigger (admin/analyst). The job already exists from Stage 1; this just enqueues it.
- **Templates**
  - `app/templates/scenarios_index.html` — full page. Tabs nav, the Predicates grid, the Evidence list, header pill, recompute button.
  - `app/templates/_scenarios_predicate_card.html` — the collapsed card partial. Re-rendered after the inline expand swap closes back.
  - `app/templates/_scenarios_predicate_detail.html` — the expanded panel partial. Loaded lazily on click.
  - `app/templates/_scenarios_evidence_list.html` — the evidence-tab table.
  - `app/templates/_scenarios_sparkline.html` — tiny SVG sparkline partial. Reusable for the card and the detail view.
- **Static**
  - `app/static/style.css` — new section: `.predicate-card`, `.predicate-card-bars`, `.predicate-card-sparkline`, `.predicate-velocity-pill`, `.predicate-detail-panel`, `.evidence-table`, `.scenarios-tabs`. Bump cache buster.
- **Navigation**
  - `app/templates/base.html` — add a "Scenarios" entry in the top nav, between the existing entries (placement matches the order in the nav today; this spec doesn't relitigate that).
- **Verify script extension**
  - `scripts/verify_scenarios_math.py` — covers the new math functions (entropy normalization, log-odds contribution math) + a tiny dashboard smoke (predicate_summary returns N entries, sparkline downsampling at the cap).

Stage 1 + Stage 2 file structure stays untouched.

## Math additions

### Shannon entropy (normalized)

```
For state probabilities {p_1, ..., p_N}:
  H_raw = -Σ p_i * log(p_i)        # natural log; 0 when p_i = 0
  H_max = log(N)                   # uniform distribution maxes here
  H_norm = H_raw / H_max if H_max > 0 else 0

Returns float in [0, 1]:
  0 = one state has all the probability mass (no contention)
  1 = uniform distribution (maximum contention)
```

Used to sort by "most contested." Comparable across binary and ordinal predicates because of the normalization.

### Velocity (percentage points, 30-day window)

```
For each state s of predicate p:
  current_p   = current_probability   (cached on PredicateState)
  baseline_p  = probability from the snapshot closest to (now - 30 days)
                  for this (predicate, state); fall back to prior if the
                  snapshot table has nothing that old.
  velocity_pp = (current_p - baseline_p) * 100
```

The grid card shows the velocity for the **dominant state** (highest current_probability) only; the sign is signed (`+8.2pp`, `−1.1pp`). The expanded panel shows velocity per state.

### Per-row log-odds contribution

```
For one confirmed evidence e on predicate p with current half_life H:
  LR        = likelihood_table[e.direction, e.strength_bucket]
  decay     = exp(-ln(2) * (now - e.observed_at).days / H)
  contrib   = log(LR) * e.credibility * decay
```

This is the exact value `compute_posterior` adds to the target state's logit. Shown in the evidence drill-down so movement is fully attributable: a strong-support row at full credibility today contributes ~`log(3) ≈ 1.10`; the same evidence one half-life ago contributes ~0.55. A contradict-strong is `~−0.92`. The drill-down sorts evidence by absolute contribution descending so the dominant drivers are at the top.

## Sparkline rendering

Pure inline SVG. No JS, no hover, no axes. Stage-3 conventions:

- **Card sparkline**: only the dominant state's probability over the last 90 days. ~120px wide × 32px high, polyline + faint baseline at 0.5. Color by ordinal_position (CSS variable; ordinal 0 = first state's color, etc).
- **Detail sparkline**: all N states stacked or overlaid (overlaid in v1 — simpler, keeps the same aspect ratio). 100% width, 80px high. Tiny legend underneath: `▢ platform  ▢ agent` etc.
- **Density throttle**: if the snapshot table has >200 rows for this (predicate, state) pair in the last 90 days, sample down to ~100 evenly spaced points before rendering. Cheap O(N) walk.
- **Empty state**: if there are zero snapshots, render a flat dashed line at the prior probability with the label "no data yet". Sets expectations without being noisy.

The `_scenarios_sparkline.html` partial takes one argument: `series` = `list[(timestamp, probability)]`. Color, width, and height are controlled by parent CSS, not the partial.

## Layout

### Page header

```
Scenarios
  ┌──────────────────────────────────────────────────────────────────┐
  │ Predicates · Scenarios · Evidence · Settings    [ Recompute ]   │
  └──────────────────────────────────────────────────────────────────┘
  [ ⓘ Computed assuming predicate independence ]
  N predicates active · M confirmed evidence · last recompute 2m ago
```

- Tabs: **Predicates** (this stage), **Scenarios** (404 — Stage 4), **Evidence** (this stage), **Settings** (404 — Stage 5).
- Recompute button: admin/analyst. Triggers `POST /api/runs/scenarios-recompute`. Returns 202 + queue position; the page polls `/runs` for status (or just shows a toast).
- Independence pill is informational; clicking it links to the spec section.

### Predicates tab — sort/filter row

```
Sort: [ Most shifting ▾ ]   Category: [ All ▾ ]   Show only: [ □ no recent evidence ]
```

Sort options: most shifting (by `|velocity_pp|` desc), most contested (by entropy desc), alphabetical (by key asc), by category (groups by category, alpha within).

Filter "no recent evidence": predicates with zero confirmed evidence in the last 30 days. Useful coverage-gap surface.

### Predicate card (collapsed)

```
┌─ p1 · Distribution control: platform vs agent ─────── discovery ─┐
│                                                                  │
│  platform   ████████████░░░░░░░  55%                             │
│  agent      ████████░░░░░░░░░░░  45%                             │
│                                                                  │
│  ╱╲╱──────╮                                                      │
│           ╰──── ─                                                │
│                                                                  │
│  3 confirmed evidence  ·  +1.4pp (30d)  ·  updated 12h ago       │
│  ▸ expand                                                        │
└──────────────────────────────────────────────────────────────────┘
```

Probability bars per state: width = current_probability, label inside if it fits else outside. Color from a 6-color palette indexed by ordinal_position.

Velocity pill: color-coded — neutral grey if `|v| < 1pp`, green if positive, amber if negative (orientation is "more probability mass on this state"; doesn't imply value judgement).

Expand affordance: clicking anywhere on the card (except links) HTMX-loads the detail partial into the panel below the card and toggles `aria-expanded`.

### Predicate card (expanded inline)

Below the collapsed card stays visible at top; a panel slides in below it:

```
┌─ Detail: p1 ─────────────────────────────────────────────────────┐
│                                                                  │
│  per-state sparklines (overlaid, 100% width × 80px)              │
│  ▢ platform  ▢ agent                                             │
│                                                                  │
│  Per-state velocity (30d):                                       │
│   platform −1.4pp    agent +1.4pp                                │
│                                                                  │
│  Evidence (sorted by absolute log-odds contribution):            │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ +1.10   2026-04-22  agent  support / strong  cred 1.00    │  │
│  │         "OpenAI Operator launch — agent-as-distribution…" │  │
│  │         finding #4521 (manual)                            │  │
│  ├────────────────────────────────────────────────────────────┤  │
│  │ −0.92   2026-04-09  agent  contradict / strong  cred 0.85 │  │
│  │         "Indeed earnings: 'web is still our front door'"  │  │
│  │         finding #4380 (llm → user_override)               │  │
│  ├────────────────────────────────────────────────────────────┤  │
│  │ +0.21   2026-03-15  agent  support / weak  cred 0.60      │  │
│  │         "ChatGPT job search browser extension survey"     │  │
│  │         finding #4112 (llm)                               │  │
│  └────────────────────────────────────────────────────────────┘  │
│  ▸ Show pending + rejected (4 hidden)                            │
│                                                                  │
│  [ Collapse ]                                                    │
└──────────────────────────────────────────────────────────────────┘
```

- "Show pending + rejected" toggle reveals `classified_by="llm"` (with confirmed_at IS NULL) and `classified_by="user_rejected"` rows in a muted style. They show 0.00 contribution.
- Each evidence row links to the source finding via the existing `/competitors/{id}` deep-link (or just `/stream?finding={id}` if a single-finding view is added later).
- Predicate ID and current state breakdown are not repeated — the collapsed card directly above already shows them.

### Evidence tab

A flat, sortable table of every confirmed evidence row across all predicates. Columns: observed_at, predicate (key), target_state, direction, strength, credibility, log-odds contribution (today), source finding link, classified_by chip.

Pagination at 200 rows; client-side sort by clicking column headers (re-renders the partial with `?sort=field`).

Useful for "show me everything that's contributed in the last week" sweeps and for catching the "one source dominates everything" failure mode the PRD §10 calls out.

## Routes

```
GET  /scenarios                                → scenarios_index.html, default tab
GET  /scenarios?tab=evidence                  → same template, evidence panel
GET  /scenarios/evidence                      → alias for the above (deep link)
GET  /scenarios/predicates/{key}/expand       → _scenarios_predicate_detail.html (HTMX partial)
POST /api/runs/scenarios-recompute            → enqueue Run kind="scenarios_recompute"
```

All auth-gated to `admin | analyst | viewer`. Recompute is admin/analyst.

## Configurability

Nothing new this stage. Existing knobs (likelihoods, credibility, decay, priors, weights) still SQL. Stage 5 turns them into forms. The dashboard reads them dynamically — adjust a row in `evidence_likelihood_ratios` and the "log-odds contribution" column on the next page load reflects it.

## Known limitations

1. **Sparkline downsampling drops detail.** Flat segments may hide small wiggles. Acceptable for v1; full-fidelity history is in the snapshot table for anyone who wants to query it.
2. **No "snapshot at confirm-time" for evidence rows.** The contribution column shows what each evidence is worth *today* (with current decay). It doesn't show what it was worth when first confirmed. A future "evidence history" view could add this; for now the live value is the more useful number.
3. **Evidence tab is one-page-at-a-time.** No filter UI, just sort + paginate. Stage 5 adds filters by predicate / source / classified_by.
4. **Empty state is sparse.** With no evidence yet, every card just shows priors and "no data yet" sparklines. Fine for a fresh install; possibly needs a "Get started: tag findings on /stream" hint banner for first-run polish.
5. **No A/B for sort defaults.** Default sort is "most shifting" — opinionated. If the team wants alphabetical to be default, that's one constant in `dashboard.py`.

## Testing

`scripts/verify_scenarios_math.py` extended:

- **Math: entropy normalization**
  - 2-state uniform → 1.0
  - 3-state uniform → 1.0 (proves normalization works across N)
  - 2-state {1.0, 0.0} → 0.0
  - 3-state {0.5, 0.3, 0.2} → some intermediate value, sanity-bounded (0, 1).

- **Math: log_odds_contribution**
  - support/strong, credibility 1.0, age 0 → `log(3.0)` exactly.
  - support/strong, credibility 0.5, age 0 → `0.5 * log(3.0)`.
  - support/strong, credibility 1.0, age = half_life → `0.5 * log(3.0)`.
  - contradict/strong → `log(0.4)`, negative.
  - neutral/anything → 0.0.

- **Service: predicate_summary returns one entry per active predicate**
  - With a fresh seed (no evidence), every entry has `velocity_pp_30d == 0.0` and a sparkline of length 0 or 1.
  - After a strong-support evidence on p8, that entry's velocity_pp_30d is non-zero and the sparkline has at least one point.
  - Inactive predicates are excluded.

- **Service: predicate_detail returns evidence sorted by |contribution| desc**
  - Insert two confirmed evidences on p1: one strong-support (~1.10), one weak-contradict (~−0.22).
  - `predicate_detail(db, "p1").evidence` lists strong-support first.

- **Service: evidence_list paginates and excludes pending/rejected**
  - Seed N evidence rows: M confirmed, K pending, L rejected.
  - `evidence_list(db, limit=10)` returns at most 10 rows, all confirmed.

- **Sparkline downsampling**
  - Synthesize 500 snapshot rows for one predicate-state pair across 90 days.
  - `predicate_summary` returns a sparkline with ≤100 points.

## Acceptance criteria

1. `/scenarios` renders without errors against a fresh seed (no evidence).
2. Tab navigation between Predicates and Evidence works without a full page reload (HTMX swap).
3. Predicates grid renders one card per active predicate; inactive predicates are hidden.
4. Sort dropdown changes the order without a full reload.
5. "no recent evidence" filter hides predicates with confirmed evidence in the last 30 days.
6. Clicking a card expands the detail panel inline; clicking again (or the Collapse button) hides it.
7. Detail panel sparklines render correctly with 0, 1, and >100 snapshots.
8. Detail panel evidence table sorts by absolute log-odds contribution descending; "show pending + rejected" reveals additional rows.
9. Evidence tab renders a flat list of confirmed evidence; sortable columns work; pagination at 200 rows.
10. Recompute button triggers a `scenarios_recompute` Run; on completion the page reflects the new posteriors after a refresh.
11. Independence-assumption pill renders in the header.
12. CSS cache buster bumped; no visual regressions to `/stream` or `/market`.
13. Stage-1 verify script + Stage-2 extensions still pass; new sections all green.
14. With `ANTHROPIC_API_KEY` unset and zero evidence, page still renders cleanly (everything degrades to priors).

## What this unblocks

- **Spec 04 — Scenario dashboard.** With the Predicates tab + visual conventions in place (cards, bars, sparklines, attribution table), the Scenarios tab inherits all of it. Add a "for each scenario, show contributing predicates" panel + sensitivity bars on top.
- **Spec 05 — Assumption controls.** The current read-only displays of priors, weights, and likelihoods become editable forms. Same templates, plus an `<input>` and a save handler. Audit log of edits.
- **Spec 06 — Belief-shift digest.** Daily/weekly email of "predicates that moved >5pp + which scenario probabilities followed." All the data is already in `predicate_posterior_snapshots`; the digest is a pure read job.
