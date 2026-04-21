# Spec 02 — Preference Rollup

**Status:** Draft
**Owner:** Simon
**Depends on:** [01 — Signal Log](01-signal-log.md)
**Unblocks:** 03 (ranker), 04 (preference chat)

## Purpose

Turn the append-only event log into a compact, explainable per-user preference profile that the ranker (spec 03) can query cheaply. This spec defines *how* events become weights and *where* those weights live.

## Non-goals

- Scoring findings (spec 03 — this spec only produces the profile, not the rank).
- LLM-written taste doc maintenance (spec 04 — this spec defines the *slot* for it, nothing more).
- Semantic embeddings. Deliberately deferred; the schema reserves a clean swap point.

## Design principles

1. **Deterministic, pure arithmetic.** No LLM in the rollup path. Running it twice = identical output.
2. **Rebuildable from events alone.** If the vector table is dropped, the next nightly run reconstructs it. The log is the source of truth; the vector is a cache.
3. **Explainable by construction.** Every weight carries `evidence_count`, `positive_count`, `negative_count`, `last_event_at` so the ranker can cite provenance.
4. **Weights live here, not in events.** Retuning the weight mapping is a config-only change + a rollup re-run — no backfill of events.
5. **Sparse tag vector today, ready for dense embeddings tomorrow.** Dimension/key model accommodates structured taste; a future embedding dimension slots in without schema churn for consumers.

## Schema

### `user_preferences_vector`

Sparse per-user preference rows across structured dimensions.

| column           | type       | null | notes                                                      |
| ---------------- | ---------- | ---- | ---------------------------------------------------------- |
| `user_id`        | fk users   | no   | `ON DELETE CASCADE`. Part of PK.                           |
| `dimension`      | string(32) | no   | Enum: `competitor` \| `signal_type` \| `source` \| `topic` \| `keyword`. Part of PK. |
| `key`            | string(128)| no   | The specific value within the dimension (e.g. `"Indeed"`). Part of PK. |
| `weight`         | float      | no   | `tanh(raw_sum)`, clamped to [-1, +1]. Ranker reads this.  |
| `raw_sum`        | float      | no   | Unsquashed decayed sum. Kept for debugging / retuning.     |
| `evidence_count` | int        | no   | Total contributing events (positive + negative).           |
| `positive_count` | int        | no   | Events with positive contribution.                         |
| `negative_count` | int        | no   | Events with negative contribution.                         |
| `last_event_at`  | datetime   | no   | Most recent contributing event. Useful for "growing interest" detection. |

PK: `(user_id, dimension, key)`.
Indexes: `(user_id, dimension)` for ranker reads; `(user_id, weight DESC)` for "top interests" queries.

### `user_preference_profile`

One row per user. Holds the LLM-editable taste doc and rollup metadata.

| column              | type       | null | notes                                              |
| ------------------- | ---------- | ---- | -------------------------------------------------- |
| `user_id`           | fk users pk| no   |                                                    |
| `taste_doc`         | text       | yes  | LLM-maintained natural language. Spec 04 writes it; rollup never touches it. |
| `cold_start`        | bool       | no   | True until `event_count_30d >= COLD_START_THRESHOLD`. |
| `event_count_30d`   | int        | no   | Cached from last rollup. Cheap cold-start check.   |
| `last_computed_at`  | datetime   | yes  | When the vector was last rebuilt.                  |
| `schema_version`    | int        | no   | Bumped when weight mapping changes meaningfully. Triggers forced rebuild. |

## Event → weight mapping

Each event contributes a **base weight** to every structured dimension its finding touches — the five dimensions below. One pin on an Indeed `new_hire` finding with `source="news"`, keyword `"hiring spree"`, topic `"talent"` produces +base to all five `(dimension, key)` pairs.

Dimensions pulled from the finding:

| dimension       | field on `Finding`            | example keys                       |
| --------------- | ----------------------------- | ---------------------------------- |
| `competitor`    | `competitor`                  | `Indeed`, `LinkedIn`               |
| `signal_type`   | `signal_type`                 | `new_hire`, `product_launch`       |
| `source`        | `source`                      | `news`, `careers`, `voc_mention`   |
| `topic`         | `topic`                       | freeform LLM output                |
| `keyword`       | `matched_keyword`             | the configured keyword that hit    |

Starting values. Document-controlled — retune over time against real data. `schema_version` bumps on change.

| event_type          | base weight | notes                                                 |
| ------------------- | ----------- | ----------------------------------------------------- |
| `shown`             | 0           | Logged for CTR denominator only. No contribution.     |
| `view`              | +0.02       | Weak positive; cheap to emit.                         |
| `dwell` (<2s)       | 0           | Noise floor (matches spec 01 emission rules).         |
| `dwell` (2–10s)     | +0.05       | Computed from `value` (dwell_ms) bucket.              |
| `dwell` (≥10s)      | +0.20       |                                                       |
| `open`              | +0.30       | Click-through or expand.                              |
| `pin`               | +0.80       |                                                       |
| `unpin`             | −0.50       | Net is still slightly positive (pin +0.8 then unpin −0.5 = +0.3) — matches the fact that they bothered to engage. |
| `dismiss`           | −0.70       |                                                       |
| `undismiss`         | +0.30       | Net is slightly negative, same logic.                 |
| `snooze`            | −0.10       | Weak negative; user said "not now", not "never".      |
| `rate_up`           | +1.00       | The primary positive explicit signal. When a reason-tagged UI lands later, the same event carries `meta.reason_tags` and adds a bonus to those tags. |
| `rate_down`         | −1.00       | Symmetric to `rate_up`.                               |
| `onboarding_seed`   | +0.50       | Synthetic seed. Applied only to dimensions in `meta.topics` / `meta.competitors`. |
| `chat_pref_update`  | 0           | Does not touch the vector. Updates `taste_doc` only; may emit synthetic `rate_up`/`rate_down` events alongside if chat expresses vector-level prefs. |

`more_like_this` / `less_like_this` stay in the event taxonomy (spec 01) but carry **no weight in v1** — we deliberately don't fire them from the current UI. When a reason-tagged interaction lands later, thumbs are likely to absorb that role rather than being distinct events, but leaving them in the taxonomy keeps the door open without requiring another migration.

## Decay

Exponential. Contribution to today's weight:

```
contribution = base_weight * exp(-ln(2) * age_days / HALF_LIFE_DAYS)
```

- `HALF_LIFE_DAYS = 30`. Event weight halves every 30 days. At 180 days (retention boundary) contribution is ~1.5% of base.
- Rollup reads events from the last 180 days only (matches spec 01 retention).

## Normalization

`raw_sum` is the signed decayed sum of contributions. `weight = tanh(raw_sum)` is what the ranker reads.

- `tanh` squashes monotonically into [−1, +1].
- Five strong positive events (~+5.0 raw) → weight ≈ +0.9999. Diminishing returns — correct.
- One strong event (+1.0 raw) → weight ≈ +0.76. Meaningful but not saturated.
- Store both `raw_sum` and `weight` so we can retune the squash later.

## Cold start

`cold_start = event_count_30d < COLD_START_THRESHOLD`. Starting threshold: **50 events**. Ranker (spec 03) uses this flag to switch to fallback ordering (recency + global popularity). Synthetic `onboarding_seed` events count toward the threshold, so a user who completes onboarding is typically warm on day one.

## Rollup job

New function: `app/ranker/rollup.py::rebuild_user_preferences(user_id)`.

Algorithm per user:

1. Read all events in the last 180 days. Order irrelevant (sum is commutative).
2. Initialize accumulators: `raw_sum[dimension][key]`, counts, `last_event_at[dimension][key]`.
3. For each event:
   - Resolve `base_weight` from the mapping (apply dwell bucketing if `event_type = dwell`).
   - Compute `decayed = base_weight * exp(-ln(2) * age_days / 30)`.
   - Load the finding's dimensions (`competitor`, `signal_type`, `source`, `topic`, `matched_keyword`). Skip nulls.
   - For each `(dimension, key)`: add `decayed` to `raw_sum`, increment `evidence_count`, increment `positive_count` or `negative_count` per sign, update `last_event_at` if newer.
4. Open a transaction:
   - `DELETE FROM user_preferences_vector WHERE user_id = ?`.
   - `INSERT` one row per non-empty `(dimension, key)` with `weight = tanh(raw_sum)`.
   - Upsert `user_preference_profile` (`event_count_30d`, `cold_start`, `last_computed_at`).
   - Commit.

Per-user idempotent. Concurrent rebuilds for the same user must serialize — take a `SELECT ... FOR UPDATE` on the profile row (or in SQLite, rely on the single-writer model; document the assumption).

### Scheduling

- **Nightly batch:** registered in `app/jobs.py` as `nightly_rebuild_preferences`. Runs at 02:00 local. Loops over active users, calls `rebuild_user_preferences(user_id)` for each. Single-threaded (SQLite).
- **Incremental trigger:** explicit high-intent events (`rate_up`, `rate_down`, `chat_pref_update`) enqueue an immediate single-user rebuild. Debounced — if a rebuild is already scheduled for this user in the next 60s, coalesce.
- **Schema-version bump:** sets `cold_start = true` and `last_computed_at = NULL` on every profile; next access triggers a rebuild. Acceptable nightly-job lag; no need for a blocking full sweep.

## On-demand recompute

`POST /api/preferences/me/rebuild` (authenticated) triggers an immediate rebuild for the calling user. Used by the preference chat after committing a change, and by debugging UI. Rate-limited to 1 request per 10s per user.

## Read API

### `GET /api/preferences/me`

Returns:

```json
{
  "user_id": 1,
  "cold_start": false,
  "event_count_30d": 142,
  "last_computed_at": "2026-04-21T02:03:11Z",
  "taste_doc": "...",
  "top": {
    "competitor":   [{"key":"Indeed", "weight":0.82, "evidence_count":17, "last_event_at":"..."}, ...],
    "signal_type":  [{"key":"new_hire","weight":0.71, ...}, ...],
    "source":       [...],
    "topic":        [...],
    "keyword":      [...]
  }
}
```

Top N per dimension: 10. Negative weights included (surface what the user dislikes too). Used by the debug UI and the preference chat.

### Internal Python API

```python
# app/ranker/preferences.py
def load_profile(db, user_id: int) -> UserProfile: ...
def dimension_weight(profile: UserProfile, dimension: str, key: str) -> float: ...
def top_keys(profile: UserProfile, dimension: str, n: int = 10) -> list[tuple[str, float]]: ...
```

`UserProfile` is a dataclass holding the vector as a dict-of-dicts plus `taste_doc`, `cold_start`, metadata. Ranker (spec 03) reads via this interface only — do not inline SQL in the scorer.

## Embedding swap point

Schema is ready for a future `dimension = "embedding"` or a parallel `user_preference_embeddings` table (dense vectors) without changing the ranker contract:

- Scorer takes a `UserProfile` + a `Finding`, returns `(score, reasons[])` (spec 03).
- A future embedding-aware scorer queries the dense vector alongside the sparse one and combines. The `UserProfile` dataclass can grow a `.embedding` field; consumers opt in.
- Nothing in this spec creates embedding tables. The seam exists at the `UserProfile` boundary.

## Performance expectations

- Per-user rebuild: ≤ 2s at 10k events (typical user should be well under).
- Nightly sweep for 100 active users: ≤ 5 minutes end-to-end.
- `GET /api/preferences/me`: ≤ 50ms (covered by `(user_id, dimension)` index plus `(user_id, weight DESC)`).

## Privacy / hygiene

- No finding content in the vector. Only structured attribute keys (competitor names, signal types, topic strings, keywords). These are already user-visible data.
- `taste_doc` may contain user preference language. Treated as user PII — not logged in plaintext, not returned over non-authenticated endpoints, not included in usage telemetry.

## Open questions (resolve before spec 03)

1. **Should `topic` be rolled up at all?** Finding `topic` is freeform LLM output — risks sparse, noisy keys. Options: (a) skip for v1, (b) include and let long-tail rows accumulate, (c) require a minimum `evidence_count` threshold before topic keys are used by the scorer. Proposal: (b) — collect, filter on read.
2. **Negative-only weight handling.** If a dimension has no positive events but several negative ones, the ranker should *penalize* matching findings, not just ignore them. This spec supports it (weight < 0 is a real value). Flagging for spec 03 to actually use it.
3. **Multi-user team signal.** If two users on the same team both pin the same finding, should that cross-pollinate? Out of scope for v1 (per-user only), but worth noting: the schema doesn't preclude a team-rollup later.

## Acceptance criteria

1. Alembic migration creates `user_preferences_vector` and `user_preference_profile`.
2. `app/ranker/rollup.py` implements `rebuild_user_preferences(user_id)` per algorithm above. Unit tests with synthetic event sequences verify correct weights, decay behavior, and sign accounting.
3. `app/jobs.py` registers `nightly_rebuild_preferences` (02:00) and the debounced incremental trigger.
4. `app/routes/signal_events.py` (from spec 01) triggers incremental rebuild on explicit-signal event types. Debounce confirmed with a test that fires 5 rapid events → exactly 1 rebuild runs.
5. `GET /api/preferences/me` and `POST /api/preferences/me/rebuild` routes in a new `app/routes/preferences.py`. Rate limit enforced.
6. `app/ranker/preferences.py` Python interface implemented and documented. Ranker (spec 03) will be required to use it.
7. Cold-start detection test: user with <50 events has `cold_start = true`; ≥50 flips to false.
8. Schema-version bump test: changing `SCHEMA_VERSION` in config forces rebuild on next access.
9. Manual smoke: generate ~100 synthetic events for one user via `/api/signals/event`, trigger rebuild, inspect `/api/preferences/me` output, sanity-check top keys match the seeded behavior.

## What this unblocks

With vectors populated, spec 03 can define the scorer against a real `UserProfile` shape. Spec 04 can define taste-doc diff format against a real `taste_doc` slot.
