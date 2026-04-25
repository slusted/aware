# Spec 08 — Semantic Ranking

**Status:** Draft
**Owner:** Simon
**Depends on:** [01 — Signal Log](01-signal-log.md), [02 — Preference Rollup](02-preference-rollup.md), [06 — Cluster & Diversity](06-cluster-diversity.md) (`default_score` lives here)
**Unblocks:** real spec 03 — semantic match becomes the strongest scorer component once warm.

## Purpose

The current scorer (`default_score` in `app/ranker/present.py`) ranks on `materiality + recency − seen_penalty` only. Spec 02's preference vector adds structured-dimension matches (competitor, signal_type, topic, keyword), but those buckets are coarse — they can't tell that "Indeed launches AI resume builder" is much closer to a user who pinned "LinkedIn ships generative cover-letter feature" than the structured dimensions alone capture.

This spec adds an `embedding_match` term to the scorer: cosine similarity between a per-user taste centroid and a per-finding embedding. The component runs **alongside** the existing rule-based and structured-dimension scoring, not in place of it. We can A/B by inspecting the `reasons[]` deltas on real users, then decide if/when to collapse `topic_match`.

## Non-goals

- Replacing `default_score` or any spec-03 component. This adds one term.
- Generating embeddings for non-finding text (taste_doc, competitor descriptions, etc). Future work.
- A vector index / `sqlite-vec` extension. 500 findings × 512-dim cosine in NumPy is sub-millisecond; we don't need ANN until the candidate set grows past tens of thousands.
- Re-embedding on every scoring run. Embeddings are computed once at extraction time and cached on the `Finding` row; centroids are computed once per rollup and cached on the profile.
- Cross-tenant or shared embedding cache. Single-tenant deploy, one user's centroid is just one user's centroid.

## Design principles

1. **Fail soft, never block ingest.** A Voyage outage or missing key must not stop a finding from being saved. Embedding is best-effort: success → row gets a vector; failure → row saves without one and the scorer's `embedding_match` term contributes 0 for that card.
2. **Additive, named, bounded.** Same shape as every other scorer term — capped contribution, listed in `reasons[]`, weight lives in `app/ranker/config.py`.
3. **Compute once, read many.** Embedding at extract time, centroid at rollup time. The ranker hot path does one packed-bytes unpack + one dot product per finding.
4. **Single vendor seam.** All Voyage calls go through one adapter so swapping providers (or adding a local fallback) is one file changed.
5. **Backfillable.** Existing findings without embeddings are filled by an idempotent script. Re-running it is safe.

## Vendor & model

- **Provider:** Voyage AI (`voyageai` SDK).
- **Model:** `voyage-3-lite` — 512 dims, ~30M tokens/$1, on par with OpenAI `text-embedding-3-small` for short retrieval. Cheap enough that backfill across all historical findings is a rounding error.
- **Input types:** Voyage distinguishes `document` (corpus text) vs `query` (search-side text). Findings embed as `document`; the user centroid is built from `document` embeddings of engaged findings, **not** a separate query embedding — keeps both vectors in the same space.

API key managed through the existing `app/env_keys.py` flow (new managed key: `VOYAGE_API_KEY`). Required: yes — without it, `embedding_match` is silently disabled and the scorer falls back to today's behaviour.

## Storage

### `Finding.embedding`

New column. Single Alembic migration:

```
embedding         BLOB NULL                 -- packed float32, len == EMBEDDING_DIM * 4 bytes
embedding_model   VARCHAR(64) NULL          -- e.g. "voyage-3-lite" — invalidates if model changes
```

Pack as `numpy.ndarray(dtype=float32).tobytes()`, unpack with `np.frombuffer(blob, dtype=np.float32)`. No JSON, no base64 — raw bytes are 2× smaller and decode in nanoseconds.

512 floats × 4 bytes = **2 KB per finding**. At ~100k lifetime findings ≈ 200 MB — acceptable for SQLite.

### `UserPreferenceProfile.taste_embedding`

New columns on the existing per-user profile row:

```
taste_embedding              BLOB NULL          -- packed float32 centroid
taste_embedding_count        INTEGER DEFAULT 0  -- how many findings contributed
taste_embedding_model        VARCHAR(64) NULL   -- must match Finding.embedding_model to be valid
taste_embedding_updated_at   DATETIME NULL
```

The centroid lives on the profile (one row per user) rather than the vector table (many rows per user) because it's a single dense vector, not a sparse dimensional weight.

## Embedding at extract time

Hook point: where we currently write `Finding.summary` (in `app/signals/summarize.py` or its caller). After the summary lands and just before commit:

```python
text = (finding.title or "") + "\n\n" + (finding.summary or finding.content or "")
vec = voyage_embed_document(text[:8192])  # 8192 char cap; voyage-3-lite ctx is plenty
finding.embedding = vec.tobytes() if vec is not None else None
finding.embedding_model = EMBEDDING_MODEL if vec is not None else None
```

`voyage_embed_document` returns `None` on any error (network, key missing, rate limit) and logs once per failure mode. The save still proceeds.

Latency budget: Voyage p95 is ~150ms per single-text call. Scanner already does multiple LLM calls per finding, so adding one more doesn't change the shape of the work. No batching in v1 — keep the seam simple; revisit if ingest throughput becomes a bottleneck.

## Centroid at rollup time

The centroid is a long-running **taste portrait** — what shape of content this user is and isn't drawn to, in semantic space. It is **not** a "recently engaged with" snapshot. Individual findings are ephemeral; the centroid is what survives them. That framing shapes two decisions below: signed contributions (positives *and* negatives), and a single L2-normalize at the end so direction carries the signal, not magnitude.

In `rebuild_user_preferences` (already does one pass over events × findings), accumulate a second sum:

- For each event whose `event_type` has a non-zero entry in `EVENT_WEIGHTS` (the same map spec 02 uses — pin +0.80, rate_up +1.0, dismiss −0.70, rate_down −1.0, etc.) and whose `Finding.embedding` is non-NULL and matches the current `EMBEDDING_MODEL`,
- Add `signed_decayed_weight * unpack(finding.embedding)` to a running 512-dim sum.
- After the loop, L2-normalize: `centroid = sum / ||sum||₂`.

Decay reuses the existing `HALF_LIFE_DAYS = 30` from preference rollup — no new knob. The single source of truth for event weights is `EVENT_WEIGHTS` in `config.py`; this spec adds no parallel scaling.

### Why signed, not positive-only

A user dismissing "Indeed launches AI resume builder" is telling us something about *the kind of content they don't want* — not about Indeed (the competitor dimension already captures that) and not about product_launch signals (the signal_type dimension already captures that). The remaining signal — "AI feature launch story shape" — lives in the embedding space, and that's exactly where it should pull the centroid away from.

Negatives are smaller-magnitude than positives in `EVENT_WEIGHTS` (dismiss = −0.70 vs pin = +0.80), so a dismissed item moves the centroid less than a pinned one — as it should: dismiss is a noisier signal than pin.

### Why L2-normalize once at the end (not divide by weight_sum)

Cosine similarity only cares about direction. Magnitude carries no useful information once both vectors are normalized. Summing signed-weighted contributions and normalizing once preserves direction correctly and avoids the "what does it mean to divide by a signed sum" question entirely.

If the running sum has length zero (no engaged findings, or perfectly cancelling positives and negatives — extremely unlikely in practice), `taste_embedding` is set to NULL — cold-start signal.

## Scoring

New term in `default_score` (and, when spec 03 lands, in `RuleBasedScorer`):

```python
embedding_match = EMBEDDING_WEIGHT * cosine(user_centroid, finding.embedding)
                  if user_centroid is not None and finding.embedding is not None
                  else 0.0
```

Both vectors are L2-normalized, so cosine = dot product. NumPy or pure Python — at 512 dims it doesn't matter.

Weight: `EMBEDDING_WEIGHT = 0.6` initially. Picked to be slightly stronger than `materiality_boost` (0.5 in spec 03) since semantic match is a richer signal than a global quality score, but not so strong it dominates. Tunable in `config.py`.

Range: cosine ∈ [−1, +1] in theory, but with same-input-type embeddings on news content, real-world range is roughly [+0.2, +0.8]. So the term contributes roughly [+0.12, +0.48] in practice.

## Config (new constants in `app/ranker/config.py`)

```python
# ── Embedding ────────────────────────────────────────────────────────
EMBEDDING_MODEL: str = "voyage-3-lite"
EMBEDDING_DIM: int = 512
EMBEDDING_INPUT_CHAR_CAP: int = 8192     # safety cap before sending to Voyage
EMBEDDING_WEIGHT: float = 0.6            # additive scorer term
# The centroid uses the same EVENT_WEIGHTS as the structured-dimension
# rollup — every signed event with an embedded finding contributes. No
# separate event filter; if EVENT_WEIGHTS says it counts, it counts here.
```

No separate centroid half-life knob — reuse `HALF_LIFE_DAYS`.

## Backfill

`scripts/backfill_embeddings.py`:

- Selects findings with `embedding IS NULL` ordered by `created_at DESC` (newer findings matter more — process them first so the next rollup picks up signal even if the script is interrupted).
- Batches of 64 (voyage-3-lite supports up to 128, but smaller batches mean smaller blast radius on retry).
- Idempotent: re-running skips already-embedded rows.
- Logs: total processed, successes, failures, elapsed.
- Cost telemetry via `UsageEvent` rows (`provider="voyage"`, `operation="embed"`, token counts from Voyage response).

After deploy, run once to backfill. After that, the extract-time hook keeps new findings embedded.

## Adapter (`app/adapters/voyage.py`)

One file, ~80 lines. Lazy module-level client (matches `app/signals/llm_classify.py` pattern so `env_keys._refresh_module_captures` can null it on key change). Two functions:

```python
def embed_document(text: str) -> np.ndarray | None: ...
def embed_documents(texts: list[str]) -> list[np.ndarray | None]: ...
```

Both return `None` per item on error and log once per (failure-mode, hour) so a sustained outage doesn't spam logs. Records a `UsageEvent` per call (single or batch — one row, summed token counts).

## env_keys integration

Add to `app/env_keys.py`:

```python
"VOYAGE_API_KEY": "Voyage AI (embeddings for semantic ranking)",
```

And in `_refresh_module_captures`:

```python
elif name == "VOYAGE_API_KEY":
    from .adapters import voyage as _voyage
    _voyage._client = None
```

## Acceptance criteria

1. Alembic migration adds `Finding.embedding`, `Finding.embedding_model`, and four `taste_embedding*` columns to `user_preference_profile`.
2. `app/adapters/voyage.py` implements `embed_document` and `embed_documents`, returns `None` on error, records `UsageEvent` rows.
3. Finding extraction populates `embedding` + `embedding_model` when the API call succeeds, leaves them NULL otherwise — finding still saves either way.
4. `rebuild_user_preferences` writes a normalized centroid to `taste_embedding` when ≥1 positive-event finding has an embedding, NULL otherwise.
5. `default_score` reads optional `user_centroid` + `finding_embedding` parameters and adds an `embedding_match` term when both are present. Behaviour unchanged when either is missing.
6. `scripts/backfill_embeddings.py` runs to completion against a populated DB; re-running is a no-op for already-embedded rows.
7. `VOYAGE_API_KEY` is managed via `/settings/keys`; clearing it disables the new term silently.
8. Unit tests cover:
   - Adapter returns `None` on error and never raises.
   - Centroid is L2-normalized and matches a hand-computed value on a fixture mixing positive and negative events (e.g. one pin and one dismiss against orthogonal embeddings → centroid points away from the dismissed direction).
   - A pure-negative event log (only dismisses) produces a non-NULL centroid that points away from the dismissed content's direction (negatives carry information on their own).
   - `embedding_match` term equals `EMBEDDING_WEIGHT * dot(a, b)` when both vectors are L2-normalized.
   - Score is unchanged when the centroid or embedding is missing.
   - Centroid skips findings whose `embedding_model` doesn't match the current model (forward-compat for model swaps).

## Open questions

1. **Model bumps and stale embeddings.** When `EMBEDDING_MODEL` changes, all existing `Finding.embedding` rows are stale. Centroid build skips mismatched-model findings — correct but means a model swap silently degrades quality until backfill reruns. Accept this; document the runbook (bump constant → run backfill script).
2. **Blocking ingest on embedding latency.** The 150ms-per-call cost is ~1% of total per-finding work, so blocking is fine for v1. If ingest throughput ever matters, switch to a deferred queue (embed in a follow-up pass) — out of scope here.
3. **Cold start.** A user with no signed events has no centroid; their stream sees `embedding_match = 0` and scoring degrades to today's behaviour. The fallback scorer (spec 03 §"Fallback scorer") already handles cold-start UX; no special embedding-side cold-start logic needed.
4. **Long-running taste vs evolving interests.** With 30-day half-life and no other smoothing, a user's taste portrait will adapt as their engagement does. If observation suggests the centroid is too volatile (e.g. one heavy session reshapes it), the right knob is a longer half-life specifically for embeddings (e.g. 60–90 days) — kept as a future config split, not a v1 knob.

## What this unblocks

When spec 03's `RuleBasedScorer` lands, `embedding_match` slots in as one of its named components with no spec-08 changes — same `EMBEDDING_WEIGHT`, same centroid source, same explainability ("Semantically similar to your engaged content"). At that point, decide whether to keep `topic_match` or collapse it into `embedding_match` based on observed reason-list deltas.
