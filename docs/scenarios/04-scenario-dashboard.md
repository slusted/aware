# Spec 04 — Scenario Dashboard + Chat Tools

**Status:** Draft
**Owner:** Simon
**Depends on:** [Spec 01](./01-foundation.md), [Spec 02](./02-card-tagging.md), [Spec 03](./03-predicate-dashboard.md).
**Unblocks:** Spec 05 (assumption controls), and any chat-driven analysis ("which predicates are pulling toward Scenario B?", "explain why Scenario C dropped").

## Purpose

Stages 1–3 built the engine and exposed predicates. The piece you actually look at to make decisions — the live distribution over **scenarios**, with attribution back to the predicates driving it — is still missing. This stage fills that.

But more importantly: **this is the spec where we enforce the headless / UI split that lets the chat agent reason over the engine.** Every datum the new Scenarios tab renders comes from a pure service function that takes `(db, ...args)` and returns a JSON-serialisable structure. Those same functions are wrapped as chat tools registered in `app/chat/tools.py`'s `TOOLS` list — so the chat agent can ask the same questions the dashboard answers, and get the same data, with no code duplication.

This principle (service-first, UI thin, chat reuses service) becomes the standing rule for the remaining stages.

## Architecture: headless / UI / chat split

```
                   ┌──────────────────────────────┐
                   │   app/scenarios/dashboard.py │
                   │   app/scenarios/service.py   │
                   │   (DB-aware service layer)   │
                   │                              │
                   │   takes (db, ...args)        │
                   │   returns dicts / NamedTuples│
                   │   no HTTP, no templates      │
                   └──────────────────────────────┘
                          │              │
            ┌─────────────┘              └──────────────┐
            ▼                                            ▼
  ┌──────────────────────┐                  ┌────────────────────────┐
  │ app/routes/scenarios │                  │ app/scenarios/chat_tools│
  │ + templates/         │                  │  + app/chat/tools.py    │
  │                      │                  │                        │
  │ thin: call service,  │                  │ thin: call service,    │
  │ pass to Jinja        │                  │ return dict to model   │
  └──────────────────────┘                  └────────────────────────┘
```

Hard rules:

1. **No business logic in routes or templates.** If a route does anything beyond "call service function, hand result to template," it goes in the service layer instead.
2. **No DB calls in chat tool handlers.** Each handler is one line: `return service.fn(db, **kwargs)`. The wrapper is the schema + the role gate; the work is in the service.
3. **Service functions are JSON-friendly.** Return dicts of primitives + datetimes. NamedTuples are fine; their `_asdict()` form lands cleanly through the chat layer's existing serialisation.
4. **Service functions are testable in isolation.** No `request`, no `templates`, no fastapi imports.

## Non-goals (this stage)

- **No editing.** Same as Stage 3.
- **No "what if" sandboxes.** Sensitivity is informational, not a slider that commits hypothetical states.
- **No alerting / digest.** Belief-shift digest is Spec 06.
- **No write tools in the chat catalog this stage.** Read-only tools only.
- **No new schema.** All scenario probabilities computed on demand from existing tables.

## Where it lives

- **Service layer**
  - `app/scenarios/dashboard.py` — extended with `ScenarioSummary`, `ScenarioContribution`, `SensitivityRow`, `ScenarioDetail` namedtuples; `scenario_summary(db)`, `scenario_detail(db, key)`, `evidence_for_finding(db, finding_id)` functions.
  - `app/scenarios/service.py` — already has `scenario_probabilities` and `scenario_sensitivity` from Stage 1; no changes.
- **Chat tools**
  - `app/scenarios/chat_tools.py` — defines `SCENARIO_TOOLS: list[Tool]`. Each handler is a 1–3 line wrapper over a service function.
  - `app/chat/tools.py` — single import + `TOOLS.extend(SCENARIO_TOOLS)`.
- **Routes** (extend `app/routes/scenarios.py`)
  - `GET /scenarios?tab=scenarios` — populates `scenario_summaries`.
  - `GET /scenarios/scenarios/{key}/expand` — HTMX partial.
- **Templates**
  - `app/templates/scenarios_index.html` — replaces the Stage-3 placeholder.
  - `app/templates/_scenarios_scenario_card.html` — collapsed card.
  - `app/templates/_scenarios_scenario_detail.html` — expanded panel: contributions + sensitivity bars.
- **CSS**
  - `app/static/style.css` — `.scenario-card`, `.scenario-card-bar`, `.sensitivity-list`, `.sensitivity-bar`. Cache buster bumped.

No schema changes.

## Service additions

`ScenarioSummary` per card: `key, name, description, probability (0–1), rank, constraint_count, constraint_satisfaction, weakest_link_predicate_key + state + label + current_p`.

`ScenarioContribution` per breakdown row: `predicate_key, predicate_name, required_state_key, required_state_label, weight, current_p_required, contribution = weight × log(P), rank_within_scenario`.

`SensitivityRow` per sensitivity entry: `predicate_key, predicate_name, target_state_key, target_state_label, delta_per_pp` (∂P(scenario) per +1pp move on the target state). Computed by reusing `service.scenario_sensitivity(...)` and normalising to per-1pp from per-5pp.

## Chat tools

Five read tools, all `requires_role="viewer"`, prefixed `scenarios_*`:

| Tool | Returns | Notes |
|---|---|---|
| `scenarios_list_scenarios` | All scenario summaries | First call for "what does the engine currently believe". |
| `scenarios_get_scenario_detail` | Full breakdown + sensitivity for one scenario | Args: `scenario_key`. |
| `scenarios_list_predicates` | All predicate summaries | Mirrors the Predicates tab; sparkline series dropped from the chat payload. |
| `scenarios_get_predicate_detail` | Per-predicate evidence drill-down with log-odds | Args: `predicate_key`. |
| `scenarios_get_evidence_for_finding` | All evidence rows attached to one finding | Args: `finding_id`. |

No write tools. Recompute, evidence confirm/edit/reject stay HTTP-only.

## UI

Scenarios tab grid: one card per active scenario, sorted by probability desc. Each card: probability bar, rank chip, constraint count, weakest-link line, Expand button.

Expanded inline: contributions table sorted by `|contribution|` desc + sensitivity bars (top by `|Δ|`). Negative sensitivity bars render with a "(pulls away)" annotation and a different fill.

## Acceptance criteria

1. `/scenarios?tab=scenarios` renders one card per active scenario, sorted by probability desc.
2. Each card shows probability bar, rank, constraint count, weakest link.
3. Click Expand → inline panel with contributions + sensitivity bars.
4. Contributions row count = scenario's `links.length` from the seed.
5. Five new chat tools registered in `app/chat/tools.py::TOOLS` and visible in `tools_for_role("viewer")`.
6. No business logic in `app/routes/scenarios.py` (every handler ≤ 5 lines).
7. No DB calls in `app/scenarios/chat_tools.py` handlers.
8. CSS cache-buster bumped; no visual regressions to `/stream`, `/market`, or the Stage-3 Predicates tab.

## What this unblocks

- **Spec 05** — assumption controls inherit the same headless/UI/chat split.
- **Spec 06** — belief-shift digest is a pure read job over `scenario_summary(db)`.
- **Chat-driven analysis** — agent can answer "which predicates are pulling toward Scenario B?" using the same structured data the dashboard renders.
