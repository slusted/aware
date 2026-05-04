# Spec 01 — Scenarios Foundation (predicate engine, headless)

**Status:** Draft
**Owner:** Simon
**Depends on:** existing `findings` table (reused as evidence source via FK).
**Unblocks:** Stage 2 (LLM-assisted classification queue), Stage 3 (predicate dashboard), Stage 4 (scenario dashboard + sensitivity), Stage 5 (assumption controls + governance), Stage 6 (synthesis integration + email).

## Purpose

Today `/market` produces narrative reads — a daily Claude digest and a weekly Gemini synthesis. Both are great at compressing what *just happened*. Neither is a structured belief about what the market is *becoming*.

This spec lays the foundation for a separate `/scenarios` section that converts continuous, unstructured market signals into a live, explicit belief model of industry evolution. Findings flow in. They get mapped to **predicates** — testable claims about market structure (e.g. "distribution control shifts to agents vs platforms"). Each predicate carries a probability distribution over its states. Predicates are bundled into **scenarios** — alternative futures defined as constrained predicate states with weights. Scenario probabilities are *computed*, not stored.

The output isn't a report. It's a current distribution over future industry states, with a full evidence trail behind every movement.

This is **Stage 1 only**: data model, math, configurability surface, seed predicates/scenarios, and a headless worker. **No UI**, **no LLM mapping**. Evidence is added manually via a CLI script. The goal is to prove the math is stable on real findings replayed by hand before anything else depends on it.

## Non-goals (this stage)

- **No UI.** Predicate/scenario dashboards land in Stages 3–4. Stage 1 is inspected via SQL and the CLI.
- **No LLM mapping.** Stage 2 introduces a classification queue. Until then, evidence is hand-entered.
- **No new top-level nav entry.** The `/scenarios` route is reserved but unrouted in Stage 1. `base.html` is untouched.
- **No automatic evidence ingestion from the scanner.** Stage 1 worker only recomputes posteriors when evidence is added or settings change. No scan-time hook yet.
- **No correlation modelling between predicates.** Scenarios assume predicate independence (see §"Known limitations"). Modelling correlations is a separate effort.
- **No predicate authoring via UI.** Predicates and scenarios are seeded from a JSON file in Stage 1; UI editing is Stage 5.
- **No email digest of belief shifts.** Stage 6.
- **No integration with the Gemini market synthesis.** Stage 6 will let synthesis runs cite current predicate state.

## Design principles

1. **Predicates are the only state layer.** Probabilities live on `predicate_states.current_probability`. Scenarios are derived on read. Findings carry no probabilities of their own.
2. **Mechanical updates, no narrative overrides.** The math runs from evidence + config. The user tunes config (priors, weights, likelihood multipliers, decay, credibility). They cannot poke a posterior directly. Audit trail is automatic.
3. **Local evidence mapping.** Each piece of `predicate_evidence` links to exactly one predicate and one target state. A single finding may produce up to 2 evidence rows (PRD constraint). Enforced at the route layer, not the schema, so manual back-fills can violate it intentionally.
4. **Append-only evidence and snapshots.** Evidence rows are immutable once confirmed. Posterior recompute writes a new snapshot row rather than overwriting history. The cached `current_probability` on `predicate_states` is the materialized "latest snapshot" view.
5. **Everything configurable, nothing hardcoded.** Likelihood ratios, source credibility defaults, decay half-life, and global knobs all live in DB tables. Code reads them at compute time. Defaults are seeded; edits are logged.
6. **Reuse `findings`.** No parallel ingestion. `predicate_evidence.finding_id` is a nullable FK so manual evidence (a hand-typed observation) is also supported.
7. **Reuse `runs`.** A `kind="scenarios_recompute"` Run wraps each posterior recompute so it shows up in `/runs` like every other job.
8. **Multi-state predicates from day one.** State space is per-predicate, stored as a separate table. Binary is just N=2. Locks the door against the binary→ternary refactor surfaced by the seed scenarios (P5/P6/P8 need 3 states).
9. **Independence assumption surfaced, not hidden.** Scenario probabilities are computed under independence. The (later) UI will say so. The math stays simple and auditable; correlations can be layered in if the v1 read becomes misleading.

## Where it lives

- **Migration**
  - `alembic/versions/<new>_scenarios_foundation.py` — chained after `z2o6p7q8r9s0`. Creates 9 tables (see §"Data model").
- **Models**
  - `app/models.py` — appended classes: `Predicate`, `PredicateState`, `Scenario`, `ScenarioPredicateLink`, `PredicateEvidence`, `PredicatePosteriorSnapshot`, `EvidenceLikelihoodRatio`, `SourceCredibilityDefault`, `ScenarioSetting`.
- **Math (pure functions, no DB)**
  - `app/scenarios/__init__.py`
  - `app/scenarios/posterior.py` — log-odds update, decay, posterior-from-evidence-list, scenario probability, sensitivity. No DB calls. Easy to unit-test.
- **DB-aware service layer**
  - `app/scenarios/service.py` — load predicates + evidence + config, call `posterior.py`, write snapshots, update cached `current_probability`.
- **Worker hook**
  - `app/jobs.py::run_scenarios_recompute_job(triggered_by="manual")` — wraps `service.recompute_all()` in a `Run(kind="scenarios_recompute")`.
- **Seed**
  - `scripts/seed_scenarios.py` — idempotent. Reads `scripts/scenarios_seed.json`, upserts predicates/states/scenarios/links, and seeds the two config tables. Safe to re-run.
  - `scripts/scenarios_seed.json` — the 8 predicates / 3 scenarios / likelihood table / source credibility defaults laid out in §"Seed".
- **Manual evidence CLI**
  - `scripts/add_evidence.py` — small Click/argparse CLI. Inserts a `predicate_evidence` row and triggers a recompute for the affected predicate. The Stage-1 way to feed the engine.
- **Integrity checks**
  - `app/scenarios/integrity.py::validate_seed(db)` — verifies state-space priors sum to 1 per predicate, scenario weights sum to 1 per scenario, every `scenario_predicate_links.required_state_key` exists in `predicate_states` for that predicate. Called by `seed_scenarios.py` after upsert; non-zero exit on failure.
- **Tests**
  - `tests/scenarios/test_posterior.py` — math correctness (binary support/contradict, multi-state target, decay, neutral evidence, prior-only).
  - `tests/scenarios/test_service.py` — recompute walks evidence, writes snapshot, updates cache.
  - `tests/scenarios/test_seed.py` — seed loads cleanly, integrity check passes.
- **Docs**
  - This file.

Nothing in `app/templates/`, `app/ui.py`, or `app/static/` is touched in Stage 1.

## Data model

All nine tables created in one migration. Field comments mirror the in-code docstrings so they survive `models.py` reads.

### `predicates`

One row per testable claim about market structure.

```python
class Predicate(Base):
    __tablename__ = "predicates"
    id: Mapped[int] = mapped_column(primary_key=True)
    # Short stable identifier used in seed files and CLI ("p1", "p2", ...).
    # Not the PK so we can rename freely; unique so seed upserts work.
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    # The precise, testable statement. Long-form. Used in dashboards and
    # in the Stage-2 LLM mapping prompt — "does this finding bear on
    # this statement?" hinges on it being unambiguous.
    statement: Mapped[str] = mapped_column(Text)
    # discovery | evaluation | transaction | control_point — drives grouping
    # in the (later) UI. Free-form string so new categories don't need a
    # migration.
    category: Mapped[str] = mapped_column(String(32), index=True)
    # Per-predicate decay override. NULL = use the global default from
    # scenario_settings. Days; converted to half-life in posterior.py.
    decay_half_life_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Soft archive flag — kept rather than deleted so historical snapshots
    # remain interpretable. Inactive predicates don't contribute to scenarios.
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

### `predicate_states`

One row per state of a predicate. Binary predicates have 2 rows; ordinal have 3+.

```python
class PredicateState(Base):
    __tablename__ = "predicate_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    predicate_id: Mapped[int] = mapped_column(ForeignKey("predicates.id", ondelete="CASCADE"), index=True)
    # Stable string key referenced by scenario_predicate_links and evidence
    # ("platform", "agent", "marketplace", ...). Lower-case, no spaces.
    state_key: Mapped[str] = mapped_column(String(64))
    # Display label ("Platform-dominant", "Agent-mediated").
    label: Mapped[str] = mapped_column(String(128))
    # 0-indexed; defines order for ordinal predicates. Binary predicates
    # use 0 and 1 arbitrarily.
    ordinal_position: Mapped[int] = mapped_column(Integer)
    # Prior probability for this state. Sum across states for one predicate
    # must equal 1.0 (validated by integrity.py, not the schema).
    prior_probability: Mapped[float] = mapped_column(Float)
    # Cached "latest snapshot" probability. Recompute writes here and the
    # snapshot table simultaneously. Read path stays fast; history stays
    # complete.
    current_probability: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("predicate_id", "state_key", name="uq_predicate_states_pred_key"),
    )
```

### `scenarios`

One row per alternative future.

```python
class Scenario(Base):
    __tablename__ = "scenarios"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # "a", "b", "c"
    name: Mapped[str] = mapped_column(String(255))                          # "Aggregator dominance"
    description: Mapped[str] = mapped_column(Text, default="")              # one-line + bullet "core"
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

### `scenario_predicate_links`

The "scenario X requires predicate Y to be in state Z with weight W" join.

```python
class ScenarioPredicateLink(Base):
    __tablename__ = "scenario_predicate_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"), index=True)
    predicate_id: Mapped[int] = mapped_column(ForeignKey("predicates.id", ondelete="CASCADE"), index=True)
    # Which state this scenario requires. Must reference a real state for
    # this predicate (validated by integrity.py).
    required_state_key: Mapped[str] = mapped_column(String(64))
    # Importance of this constraint within the scenario. Sum across links
    # for one scenario must equal 1.0 (validated by integrity.py).
    weight: Mapped[float] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("scenario_id", "predicate_id", name="uq_scenario_predicate"),
    )
```

### `predicate_evidence`

One row per piece of evidence. Append-only after `confirmed_at` is set.

```python
class PredicateEvidence(Base):
    __tablename__ = "predicate_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    # Source finding. Nullable so manual evidence (typed-in observation,
    # not from the scanner) is also supported. ON DELETE SET NULL so a
    # finding prune doesn't strand the belief history.
    finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("findings.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    predicate_id: Mapped[int] = mapped_column(ForeignKey("predicates.id", ondelete="CASCADE"), index=True)
    # Which state of the predicate this evidence bears on. Required —
    # multi-state predicates need a target.
    target_state_key: Mapped[str] = mapped_column(String(64))
    # "support" | "contradict" | "neutral". "neutral" exists so an LLM
    # can say "this finding mentions the predicate but doesn't move it".
    direction: Mapped[str] = mapped_column(String(16))
    # "weak" | "moderate" | "strong" — joined to evidence_likelihood_ratios
    # at compute time. Bucket rather than free float so tuning is one-table.
    strength_bucket: Mapped[str] = mapped_column(String(16))
    # 0–1 scalar. Defaults to source_credibility_defaults.credibility for
    # the finding's source_type at insert time; can be overridden per row.
    credibility: Mapped[float] = mapped_column(Float, default=1.0)
    # "manual" | "llm" | "user_override". Stage 1 is "manual" only.
    classified_by: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    # When the evidence was actually observed (defaults to finding's
    # published_at or created_at). Drives decay. Separate from
    # confirmed_at so backdating manual evidence works.
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    # Set when a human (or LLM-then-human in Stage 2) confirms the
    # mapping. Only confirmed evidence contributes to posteriors.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Free-form note from whoever classified — useful for "I overrode the
    # LLM because…" later.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_predicate_evidence_pred_observed", "predicate_id", "observed_at"),
    )
```

### `predicate_posterior_snapshots`

Append-only history. Sparkline source. One row per (predicate, state, recompute).

```python
class PredicatePosteriorSnapshot(Base):
    __tablename__ = "predicate_posterior_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    predicate_id: Mapped[int] = mapped_column(ForeignKey("predicates.id", ondelete="CASCADE"), index=True)
    state_key: Mapped[str] = mapped_column(String(64))
    probability: Mapped[float] = mapped_column(Float)
    # Total evidence count contributing at this snapshot. Lets us spot
    # "moved a lot but only 1 evidence behind it" cases.
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    # Optional Run id when the recompute was triggered by a job.
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_posterior_snap_pred_computed", "predicate_id", "computed_at"),
    )
```

### `evidence_likelihood_ratios`

The (direction, strength) → multiplier table. Editable. Every edit logged via `updated_at` + an audit RunEvent on save (Stage 5; in Stage 1 you edit via SQL and the recompute Run captures the recompute).

```python
class EvidenceLikelihoodRatio(Base):
    __tablename__ = "evidence_likelihood_ratios"
    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String(16))         # support | contradict | neutral
    strength_bucket: Mapped[str] = mapped_column(String(16))   # weak | moderate | strong
    multiplier: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("direction", "strength_bucket", name="uq_likelihood_dir_strength"),
    )
```

Seeded values (from PRD §4.1):

| direction | strength | multiplier |
|---|---|---|
| support | strong | 3.0 |
| support | moderate | 1.8 |
| support | weak | 1.2 |
| neutral | weak | 1.0 |
| contradict | weak | 0.8 |
| contradict | strong | 0.4 |

(`neutral × moderate/strong` and `contradict × moderate` rows are seeded with `multiplier=1.0` / interpolated values for completeness so the join never misses; tunable later.)

### `source_credibility_defaults`

Per-source default credibility. Used to populate `predicate_evidence.credibility` at insert.

```python
class SourceCredibilityDefault(Base):
    __tablename__ = "source_credibility_defaults"
    id: Mapped[int] = mapped_column(primary_key=True)
    # Matches Finding.source values: "tavily", "serper", "voc", "ats",
    # "newsroom", "manual", ...
    source_type: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    credibility: Mapped[float] = mapped_column(Float)  # 0–1
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

Seeded: `manual=1.0`, `voc=0.85`, `ats=0.85`, `newsroom=0.75`, `tavily=0.6`, `serper=0.6`. Tunable.

### `scenario_settings`

Key-value bag for global knobs. JSON value so each setting can have whatever shape it needs.

```python
class ScenarioSetting(Base):
    __tablename__ = "scenario_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

Seeded keys:
- `default_decay_half_life_days` → `{"value": 60}`
- `independence_assumption_acknowledged` → `{"value": true}` (Stage-3 UI will display the disclaimer based on this)
- `min_evidence_for_movement` → `{"value": 0}` (Stage-1 default; Stage 5 may bump it to require ≥N evidence before treating a posterior as meaningful)

## Math

All in `app/scenarios/posterior.py`. No DB. Pure functions.

### Per-predicate posterior

For each predicate, walk its confirmed evidence and accumulate a log-likelihood increment on the **target state only**, then softmax against the priors. This is the standard Bayesian form: each evidence has likelihood `LR` for the state it targets and `1` for every other state, so only the target's logit moves; mass redistributes through normalization.

```
For predicate P with N states {s_1, ..., s_N} and prior π_i = prior(s_i):

  initial logit(s_i) = log(π_i)

  For each confirmed evidence e for P:
      LR        = evidence_likelihood_ratios[e.direction, e.strength]
      decay     = exp(-ln(2) * (now - e.observed_at).days / half_life_days(P))
      weight    = log(LR) * e.credibility * decay

      logit(e.target_state_key) += weight
      # Other states are unchanged — softmax over the priors handles redistribution.

  Softmax (numerically stable):
      m       = max_j logit(s_j)
      p(s_i)  = exp(logit(s_i) - m) / Σ_j exp(logit(s_j) - m)
```

Notes:
- For binary predicates this reduces exactly to the standard log-odds form: `posterior_odds(target) = prior_odds(target) × LR^(credibility × decay)`.
- Multi-state: support for one state of an N-state predicate raises that state's mass; the other (N−1) states' masses fall in proportion to their existing probabilities. No explicit drain term — normalization does the redistribution.
- A `direction="neutral"` evidence has `LR=1.0`, so `log(LR)=0`. No movement, but it's recorded — useful as proof the predicate was *seen* and considered, even if not moved.
- A `direction="contradict"` evidence has `LR<1`, so `log(LR)<0`. The target state's logit decreases; mass flows to the other states proportionally to their priors.
- Reinforcement is sublinear by construction: each new supportive evidence adds a fixed amount to the logit, but probability saturates at 1 via softmax. No extra dampening needed in Stage 1. If we see source over-concentration in practice, Stage 5 adds per-source dampening.

### Scenario probability

```
For scenario S with links {(P_j, required_state_j, weight_j)}:

  unnormalized(S) = Π_j  P(P_j = required_state_j) ^ weight_j

  Normalize across all active scenarios:
    P(S_k) = unnormalized(S_k) / Σ_m unnormalized(S_m)
```

This is the **independence-assumption** form. Documented as a known limitation (§"Known limitations"). Stage 4 surfaces it in the UI.

### Sensitivity

For dashboard "what moves the needle":

```
sensitivity(S, P) = ∂P(S) / ∂P(P = required_state_for_S)
```

In Stage 1 this is a numerical derivative — bump the predicate's probability for that state by ±0.05, recompute scenario probabilities, report the delta. Cheap, accurate enough.

## Seed

`scripts/scenarios_seed.json`. Idempotent: `seed_scenarios.py` upserts by `key`, never deletes.

### Predicates (8) with state spaces

| key | category | states (`state_key`: prior) |
|---|---|---|
| p1 | discovery | `platform: 0.55`, `agent: 0.45` |
| p2 | discovery | `horizontal: 0.55`, `vertical: 0.45` |
| p3 | evaluation | `implicit: 0.50`, `explicit_continuous: 0.50` |
| p4 | evaluation | `human_led: 0.40`, `model_led: 0.60` |
| p5 | evaluation | `optional: 0.40`, `required_centralized: 0.30`, `embedded: 0.30` |
| p6 | transaction | `ads: 0.45`, `hybrid: 0.30`, `outcomes: 0.25` |
| p7 | transaction | `multi_step: 0.55`, `compressed: 0.45` |
| p8 | control_point | `marketplace: 0.40`, `external_interface: 0.25`, `workflow: 0.35` |

Priors are deliberately near 50/50 (binary) or near uniform (ternary) — no claim of strong prior knowledge in v1. Adjust freely; Stage 5 makes this a UI knob.

### Scenarios (3) with predicate links

**Scenario A — Aggregator dominance:**
- p1 → `platform` weight 0.25
- p2 → `horizontal` weight 0.20
- p5 → `required_centralized` weight 0.15
- p6 → `ads` weight 0.15
- p8 → `marketplace` weight 0.25

**Scenario B — Agent-mediated market:**
- p1 → `agent` weight 0.30
- p3 → `explicit_continuous` weight 0.15
- p4 → `model_led` weight 0.20
- p6 → `outcomes` weight 0.20
- p8 → `external_interface` weight 0.15

**Scenario C — Workflow / ATS dominance:**
- p4 → `model_led` weight 0.15
- p5 → `embedded` weight 0.15
- p6 → `outcomes` weight 0.20
- p7 → `compressed` weight 0.15
- p8 → `workflow` weight 0.35

Weights sum to 1 per scenario. Integrity check confirms.

## Worker / job flow

Stage 1 has one job and one CLI:

1. **`run_scenarios_recompute_job(triggered_by, predicate_keys=None)`** — `app/jobs.py`:
   - Creates a `Run(kind="scenarios_recompute", triggered_by=...)`.
   - Loads predicates (filtered to `predicate_keys` if provided, else all active).
   - For each predicate, calls `service.recompute_predicate(predicate_id)`:
     - Loads confirmed evidence (with finding join for `observed_at`).
     - Loads likelihood ratios + decay setting.
     - Calls `posterior.compute(prior, evidence, likelihood_table, half_life_days)` → `dict[state_key, probability]`.
     - Writes a new `predicate_posterior_snapshot` row per state.
     - Updates cached `predicate_states.current_probability`.
   - Logs a `level="info"` RunEvent with `{predicates_recomputed, evidence_seen, max_movement}`.
   - On any exception, marks the Run `error`. No partial commits — recompute is per-predicate transactional.

2. **`scripts/add_evidence.py`** — argparse CLI:
   ```
   python -m scripts.add_evidence \
     --predicate p8 \
     --target-state workflow \
     --direction support \
     --strength strong \
     --finding-id 12345 \
     --notes "Workday earnings call: 'job distribution is the #1 ask from customers'"
   ```
   - Looks up finding's `source` to default credibility (overridable via `--credibility 0.7`).
   - Defaults `observed_at` to finding's `published_at` (or `--observed-at YYYY-MM-DD`).
   - Inserts the row, sets `confirmed_at=now`, `classified_by="manual"`.
   - Triggers `run_scenarios_recompute_job(triggered_by="cli", predicate_keys=["p8"])` synchronously. Prints before/after probability dict.

3. **No automatic recompute on scan completion in Stage 1.** Adding the scanner hook is a Stage-2 deliverable, after we trust the LLM mapping enough to auto-confirm. Until then, hand-classify; recompute fires per insert.

## Configurability surface

Every knob the user might want to tune lives in DB rows or JSON settings:

| Knob | Where | How to edit (Stage 1) |
|---|---|---|
| Likelihood multipliers per (direction, strength) | `evidence_likelihood_ratios` rows | SQL UPDATE; recompute via job |
| Source credibility per source_type | `source_credibility_defaults` rows | SQL UPDATE; affects new evidence at insert |
| Per-evidence credibility override | `predicate_evidence.credibility` | SQL UPDATE on the row + recompute |
| Global decay half-life | `scenario_settings["default_decay_half_life_days"]` | SQL UPDATE; recompute |
| Per-predicate decay override | `predicates.decay_half_life_days` | SQL UPDATE; recompute |
| Predicate priors | `predicate_states.prior_probability` | SQL UPDATE; integrity check; recompute |
| Predicate active flag | `predicates.active` | SQL UPDATE; affects scenario derivation |
| Scenario weights | `scenario_predicate_links.weight` | SQL UPDATE; integrity check; affects scenario derivation |

Stage 5 turns these into UI forms with audit logs. Stage 1 leaves them as raw SQL because there's no user yet — only Simon, behind the CLI.

## Known limitations (acknowledged, surfaced later)

1. **Predicate independence in scenario formula.** `Π P(predicate)^weight` assumes the predicates inside one scenario are independent. They aren't — e.g. `p1=agent` and `p4=model_led` are correlated. The v1 read may overcount when correlated predicates all move together. Mitigation: surface in UI ("computed assuming predicate independence"), keep predicates as orthogonal as possible in authoring, revisit if scenarios visibly distort.
2. **No double-counting protection across evidence rows.** Two evidence rows pointing at the same predicate from the same root cause (e.g. one news article cited by two findings) both contribute. Stage 5 may add a `dedup_key` and skip duplicates within a window.
3. **Redistribution across non-target states is prior-proportional.** When evidence targets state X, the other states' mass shifts in proportion to their existing probabilities. For ordinal predicates with semantically ordered states (e.g. P5 `optional → required_centralized → embedded`), contradicting one state should arguably push more probability toward neighbours than toward the far end. Stage 1 ignores ordinal structure — softmax-with-priors keeps the math simple. Revisit only if dashboards show implausible mass jumping across an ordinal scale.
4. **Snapshot growth.** N predicates × M states × every recompute = unbounded row growth. Fine in Stage 1 (recompute is per-evidence, low volume). Stage 4 may add a "snapshot at most every 6 hours unless movement > 0.05" throttle.
5. **No audit log table for config edits.** `updated_at` is the only trace in Stage 1. Stage 5 introduces `scenario_config_audit` rows.

## Testing

Unit tests in `tests/scenarios/` — no DB hits in `test_posterior.py`, in-memory SQLite for `test_service.py` and `test_seed.py`.

- `test_posterior.py`:
  - Prior-only (no evidence) returns priors unchanged.
  - One strong-support evidence on binary predicate moves probability the right direction.
  - One strong-support + one strong-contradict on the same target state nets to ~prior.
  - Decay: same evidence at age 0 vs age = 2× half-life produces stronger and weaker movement respectively, and the ratio matches `0.25` (two half-lives).
  - Credibility: evidence at credibility 0.5 moves the posterior half as much (in log-odds space) as the same evidence at 1.0.
  - Multi-state target: support for one state of a 3-state predicate raises that state and lowers the other two roughly evenly.
  - Neutral evidence (`direction="neutral"`) leaves the posterior unchanged.
  - Softmax outputs sum to 1.0 ± 1e-9 across all tests.

- `test_service.py`:
  - Insert seed → recompute → snapshots written, cache updated, `current_probability == prior` (no evidence yet).
  - Insert one evidence → recompute the predicate only → only that predicate's snapshot/cache changes.
  - Recompute all → exactly N predicates × M states snapshot rows added.

- `test_seed.py`:
  - `seed_scenarios.py` runs cleanly on an empty DB.
  - Re-running is idempotent: row counts stable, no duplicate rows, `updated_at` advances.
  - `validate_seed` passes on the canonical seed.
  - Mutating the seed to break a constraint (priors not summing to 1, weights not summing to 1, scenario links pointing at a non-existent state) makes `validate_seed` raise with a specific message.

- Manual smoke test (CLI):
  - Run `seed_scenarios.py` against a clean DB.
  - Add 3–5 hand-picked findings as evidence via `add_evidence.py`. Inspect `predicate_states.current_probability` and `predicate_posterior_snapshots` via SQL — values move the way intuition says they should.
  - Compute scenario probabilities ad-hoc via `python -c "from app.scenarios.service import scenario_probabilities; ..."` and sanity-check.

## Acceptance criteria

1. Migration applies cleanly forward and backward against a copy of prod.
2. `python -m scripts.seed_scenarios` on a clean DB seeds 8 predicates (with the right state counts), 3 scenarios, the likelihood table, the credibility defaults, and the settings keys. Re-running is a no-op.
3. `validate_seed` exits non-zero on any of: prior sum ≠ 1, weight sum ≠ 1, scenario link to unknown state, duplicate (predicate, state_key).
4. With no evidence, `current_probability` equals the seed prior for every state.
5. `python -m scripts.add_evidence ...` for one strong-support evidence on `p8 → workflow` moves `p8`'s `workflow` probability up and the others down, by amounts that match the formula in §"Math".
6. Two opposing strong evidences on the same target state net to ~prior (within rounding).
7. Decaying the same evidence by setting `observed_at` to 60 days ago (one half-life) produces ~half the log-odds movement of the present-day version.
8. Snapshot rows are written for every state of every recomputed predicate, with `computed_at` and `evidence_count` populated.
9. The recompute job appears in `/runs` with `kind="scenarios_recompute"`, status `ok` on success, RunEvents containing the recompute summary.
10. All unit tests pass. Coverage on `app/scenarios/posterior.py` ≥ 95%.
11. No template, route, or `base.html` change. `/scenarios` returns 404 in Stage 1.
12. Existing `/market`, scanner, and synthesis flows behave identically.

## What this unblocks

- **Stage 2 — Classification queue.** With the math + storage stable, an LLM can propose `(predicate, target_state, direction, strength)` per new finding, write rows with `classified_by="llm"` and `confirmed_at=NULL`. A small queue UI lets the user confirm/edit/reject; only confirmed evidence triggers recompute. Mapping quality is now isolable — measurable independently of math correctness.
- **Stage 3 — Predicate dashboard.** Read-only `/scenarios` page. Predicate cards with sparkline (from `predicate_posterior_snapshots`), most-shifting / most-contested, evidence drill-down. First user-facing payoff of the engine.
- **Stage 4 — Scenario dashboard + sensitivity.** `/scenarios#scenarios` view computing live P(scenario), top-contribution table, sensitivity bars. The actual product output.
- **Stage 5 — Assumption controls + governance.** Forms for editing priors, weights, likelihood multipliers, and overriding individual evidence classifications. Audit log table. Coverage diagnostics ("predicates with no evidence in 60d", "evidence concentrated in one source"). Quarterly rebaseline workflow.
- **Stage 6 — Integration.** The Gemini market synthesis brief gains a "current belief state" section pulled from the latest snapshots. Weekly email of biggest belief shifts. Scenario probabilities become a chart in the synthesis report.
