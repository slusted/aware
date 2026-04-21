# Spec 06 — Cluster & Diversity

**Status:** Draft
**Owner:** Simon
**Depends on:** [03 — Ranker](03-ranker.md) (interface-level; this spec works with or without a populated `Scored`)
**Unblocks:** better top-of-stream experience without waiting on spec 04/05

## Purpose

The stream today is one finding per row, sorted by `created_at desc`. Two complaints follow:

1. A single event (funding round, hire, product launch) arrives as five near-identical findings from five publishers.
2. The top of the stream collapses into the loudest competitor × signal type (e.g. "Indeed × new_hire" five times in a row).

This spec adds a presentation layer between the ranker and the template that (a) collapses near-duplicates into a single card with a "N sources" chip and (b) forces diversity across the top slots via MMR. Orthogonal to spec 03's scoring math — both improvements apply whether or not the warm scorer is wired in yet.

## Non-goals

- A scoring model (spec 03 owns that).
- Cross-competitor clustering. Two different competitors announcing the same-named feature is not a duplicate event — it's two events worth seeing.
- Persisted clusters. Runtime-only: clusters are recomputed per request. Avoids cache-invalidation complexity and keeps the DB schema untouched.

## Design principles

1. **Post-processing, not a rewrite.** Input = ranked `list[Finding]`, output = `list[ClusterCard]`. The ranker stays pure; this layer wraps it.
2. **Deterministic and cheap.** ≤ 20ms for 500 findings. No LLM, no DB calls, no embeddings in v1. Jaccard on title tokens is enough.
3. **Explainable.** A clustered card shows its sibling count and — on expand — the individual members. MMR placement never reorders items that weren't in the top-N window.
4. **Graceful degradation.** Clustering requires `title`; when absent, the finding is a cluster of one. MMR requires a score; when the ranker hasn't produced one, use a stand-in (materiality + recency).

## Pipeline

```
findings (DB-ordered)
   │
   ▼
┌──────────────────┐
│ score_findings() │   ← stand-in scorer until spec 03 wires the real one
└──────────────────┘
   │
   ▼
┌──────────────────┐
│ cluster()        │   ← Jaccard-title dedupe within (competitor, signal_type)
└──────────────────┘
   │
   ▼
┌──────────────────┐
│ diversify()      │   ← MMR on the top N cluster-leads
└──────────────────┘
   │
   ▼
list[ClusterCard]       → template
```

All three stages are pure functions; any one can be swapped independently.

## Clustering

### When two findings are duplicates

All of:

- Same `competitor` (case-insensitive, whitespace-trimmed).
- Same `signal_type` (or both NULL).
- `title_jaccard(a, b) >= 0.4` over normalized word tokens.

### Title normalization

- Lowercase, strip punctuation (keep alphanumerics + whitespace).
- Drop a tiny stopword list: `{a, an, the, of, to, in, on, at, and, or, for, with, by, is, are, was, were, its, it}`. Content words like "launches", "hires", "raises" stay — they're the story.
- Tokens ≥ 2 chars only. Drops the "s", "to" fragments post-punctuation strip.

### Threshold choice: 0.4

With 4–6 content words per title, Jaccard 0.4 lands at "3 of 7 words match" — empirically the knee where obvious duplicates ("Indeed launches new pricing tier" vs "Indeed rolls out new pricing") cluster together, but tangentially related titles ("Indeed launches pricing" vs "Indeed launches hiring platform") stay separate. Tunable in `config.py`.

### Algorithm

1. Bucket findings by `(competitor, signal_type)`.
2. Within each bucket, union-find over the `jaccard >= threshold` relation.
   - Pairwise cost is O(n²) per bucket, but buckets are small — a single (competitor, signal_type) rarely exceeds a few dozen findings in a 30-day window.
3. Cluster lead = highest score, ties broken by newest `created_at`.
4. Order cluster members by score desc — first member is the lead.

### What about findings without `signal_type`?

`signal_type IS NULL` bucket is its own bucket. Two unclassified findings from the same competitor can still cluster if titles match. Prevents the NULL bucket from becoming a clustering blind spot.

## Diversity — MMR

Maximal Marginal Relevance on the top `MMR_WINDOW = 20` cluster-leads. Everything below window 20 retains score-desc order.

### Standard MMR step

Greedy selection. At each step, pick the cluster that maximizes:

```
λ · score(c) − (1 − λ) · max_{p ∈ picked} sim(c, p)
```

with `λ = 0.7` (lean relevance, but meaningfully penalize similarity).

### Similarity between two clusters

Categorical overlap on the lead findings' dimensions:

| shared dim           | contribution |
| -------------------- | ------------ |
| `competitor`         | +0.5         |
| `signal_type`        | +0.3         |
| `topic`              | +0.15        |
| `source`             | +0.05        |

Sum, clamp to [0, 1]. "Same competitor + same signal_type" = 0.8 — a very strong duplicate-feeling pair; MMR pushes the second one down. Two different competitors with the same signal_type only overlap 0.3 — they can sit near each other.

### Why MMR only on top 20

Past the top 20 the user is deliberately scrolling into the long tail; diversity there is wasted compute and can break intuition (older finding suddenly above newer one for no visible reason). Top 20 is the attention window.

## Interface

```python
# app/ranker/present.py
@dataclass(frozen=True)
class ClusterCard:
    lead: Finding
    members: tuple[Finding, ...]   # always includes lead at index 0; len >= 1
    score: float

    @property
    def size(self) -> int:
        return len(self.members)

def present(
    findings: list[Finding],
    *,
    score_fn: Callable[[Finding], float] | None = None,
    now: datetime | None = None,
    mmr_window: int = 20,
    mmr_lambda: float = 0.7,
    jaccard_threshold: float = 0.4,
) -> list[ClusterCard]: ...
```

`score_fn` defaults to `default_score` (materiality + recency stand-in). When spec 03's ranker lands, callers pass `lambda f: scored_by_id[f.id].score`.

## Stand-in score

Until spec 03 wires a real scorer, use:

```
score = (materiality or 0.0) + 0.3 * exp(-ln(2) * age_days / 7)
```

- Materiality: 0–1, LLM-assigned.
- Recency: 7-day half-life bump up to +0.3 for fresh findings.

Rationale: preserves today's "new stuff first" feel while letting high-materiality older items hold their ground — exactly the shape we'd want from a minimal ranker.

## UI contract

The template receives `list[ClusterCard]` instead of `list[Finding]`. For each card:

- Render the `lead` finding as today.
- If `size > 1`, show a small chip next to the source: `+{size-1} more sources`.
- Future: clicking the chip expands to show all members. Not in v1 — keeping scope tight.

Ranker signals (`shown`/`view`/etc.) log against the `lead.id` only. Members are hidden from view and shouldn't emit impressions. This is a deliberate simplification — clustered siblings are effectively suppressed for signal-log purposes. Tradeoff: we lose the ability to learn "user engages with NYT's writeup but not TechCrunch's"; we gain signal-log sanity (5× duplicate `shown` rows per event vanish).

## Configuration

Add to `app/ranker/config.py`:

```python
CLUSTER_JACCARD_THRESHOLD: float = 0.4
CLUSTER_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "of", "to", "in", "on", "at",
    "and", "or", "for", "with", "by",
    "is", "are", "was", "were", "its", "it",
})
MMR_WINDOW: int = 20
MMR_LAMBDA: float = 0.7
MMR_SIM_WEIGHTS: dict[str, float] = {
    "competitor": 0.5,
    "signal_type": 0.3,
    "topic": 0.15,
    "source": 0.05,
}
```

No DB migration, no schema change.

## Acceptance criteria

1. `app/ranker/present.py` implements `ClusterCard`, `present()`, `cluster()`, `diversify()`, `default_score()`, `title_jaccard()`.
2. Unit tests cover:
   - Two findings with near-identical titles in same (competitor, signal_type) → cluster of 2.
   - Two findings with identical titles but different competitors → two clusters of 1.
   - Three findings clustering transitively (A~B, B~C, A!~C directly) → one cluster of 3 via union-find.
   - NULL title → own cluster of 1.
   - Lead selection: highest score wins, tie broken by newest.
   - MMR: two clusters with `sim = 0.8` never occupy slots 1 and 2 when a lower-scoring dissimilar cluster exists.
   - MMR beyond `mmr_window` respects pure score-desc order.
   - Empty input → empty list.
3. `_stream_query` in `app/ui.py` calls `present()` and passes `list[ClusterCard]` to the template.
4. `_stream_list.html` and `_stream_card.html` render the `+N more sources` chip when `card.size > 1`.
5. `shown` event emission logs only the lead `id` — not member ids.
6. Smoke test: on a real DB, top 20 of the stream never contains two cards from the same (competitor, signal_type) back-to-back unless no alternatives exist.

## Open questions

1. **Expand-members on click?** v1 just shows the count. If users ask "which 5 sources?", add a popover listing titles + URLs. Low effort to add later; keeps v1 tight.
2. **Cross-competitor duplicate detection?** Two competitors announcing the same industry event (e.g. a regulatory change) might deserve collapsing. Rare enough to defer.
3. **Learning the threshold per user?** Users who hate duplicates might want 0.3; users who want to see every angle might want 0.6. A pref knob is cheap; deferred until anyone complains.

## What this unblocks

- Spec 03's eventual wiring (spec 05) plugs its `score_fn` in without template changes.
- Spec 04 (preference chat) can reference "topics the user has clustered around" as a soft signal once clustering exists in the runtime path.
