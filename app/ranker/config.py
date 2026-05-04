"""Tuning knobs for the preference rollup (docs/ranker/02-preference-rollup.md).

Every number in here is a guess until we have a week of real event data
to validate against. Retuning = edit this file, bump SCHEMA_VERSION,
next rollup picks up the change.

Do NOT put weights inside the event rows themselves — they live here so
retuning never requires backfilling the log.
"""
from __future__ import annotations


# ── Decay ────────────────────────────────────────────────────────────
# Exponential decay half-life. Contribution = base * exp(-ln(2) * age_d / HALF_LIFE).
# At 180 days (spec 01 retention boundary) contribution is ~1.5% of base.
HALF_LIFE_DAYS: float = 30.0


# ── Cold start ───────────────────────────────────────────────────────
# Below this count in the trailing 30 days, the ranker falls back to
# recency + popularity ordering instead of consulting the vector.
COLD_START_THRESHOLD: int = 50


# ── Schema version ───────────────────────────────────────────────────
# Bump when any of the numbers below change in a way that invalidates
# cached vectors. The rollup compares stored vs. current and forces a
# rebuild when they differ.
SCHEMA_VERSION: int = 1


# ── Dwell bucketing ──────────────────────────────────────────────────
# Spec 01 stores dwell as raw ms in UserSignalEvent.value. The rollup
# buckets it here because the weight is bucket-based, not linear.
DWELL_BUCKETS_MS: tuple[tuple[int, str], ...] = (
    (0, "noise"),      # 0 ≤ ms < 500  → dropped
    (500, "short"),    # 500 ≤ ms < 2000
    (2_000, "medium"), # 2000 ≤ ms < 10000
    (10_000, "long"),  # ≥10000
)


def dwell_bucket(dwell_ms: float | None) -> str | None:
    """Return 'noise' | 'short' | 'medium' | 'long' | None.

    None = missing value. 'noise' = below the 500ms floor; spec 01 tells
    clients not to emit these but we accept them defensively.
    """
    if dwell_ms is None:
        return None
    last = None
    for threshold, name in DWELL_BUCKETS_MS:
        if dwell_ms >= threshold:
            last = name
        else:
            break
    return last


# ── Event → base weight mapping ──────────────────────────────────────
# Weight applied to EACH dimension the event's finding touches (five
# dimensions: competitor, signal_type, source, topic, matched_keyword).
# A single pin on an Indeed new_hire finding contributes +0.80 to each
# matching (dim, key) pair.
#
# `dwell` is a special case — bucket-dependent, see DWELL_WEIGHTS.
# `shown` and `chat_pref_update` are weight=0 but still "valid" event
# types in the mapping (chat_pref_update updates taste_doc via spec 04,
# not this vector).
EVENT_WEIGHTS: dict[str, float] = {
    "shown": 0.0,
    "view": 0.02,
    "open": 0.30,
    "pin": 0.80,
    "unpin": -0.50,
    "dismiss": -0.70,
    "undismiss": 0.30,
    "snooze": -0.10,
    "rate_up": 1.00,
    "rate_down": -1.00,
    # In taxonomy but unused in v1 — see spec 02 §"Event → weight mapping".
    # Left at 0 so that if they ever fire (e.g. synthetic events from a
    # future flow) they're no-ops until we retune.
    "more_like_this": 0.0,
    "less_like_this": 0.0,
    "onboarding_seed": 0.50,
    "chat_pref_update": 0.0,
}

# Dwell bucket → weight. "noise" contributes nothing.
DWELL_WEIGHTS: dict[str, float] = {
    "noise": 0.0,
    "short": 0.05,
    "medium": 0.05,   # same as short — we only distinguish "engaged enough to count" vs "really engaged"
    "long": 0.20,
}


# ── High-intent event types (trigger incremental rollup) ─────────────
# Spec 02 §"Scheduling": posting one of these to /api/signals/event
# enqueues an immediate per-user rebuild so the ranker sees explicit
# feedback within seconds, not overnight.
INCREMENTAL_TRIGGER_TYPES: frozenset[str] = frozenset({
    "rate_up",
    "rate_down",
    "chat_pref_update",
})

# Debounce window for incremental rebuilds. If a rebuild is already
# scheduled for this user within this many seconds, coalesce instead of
# queueing another.
INCREMENTAL_DEBOUNCE_SECONDS: int = 60


# ── Dimensions ───────────────────────────────────────────────────────
# The attributes of a Finding that feed the vector. Order irrelevant;
# this is the closed set the rollup iterates over.
FINDING_DIMENSIONS: tuple[tuple[str, str], ...] = (
    # (dimension_name, Finding attribute)
    ("competitor", "competitor"),
    ("signal_type", "signal_type"),
    ("source", "source"),
    ("topic", "topic"),
    ("keyword", "matched_keyword"),
)


# ── Clustering & diversity (spec 06) ─────────────────────────────────
# Post-ranker presentation layer. Collapses near-duplicate findings into
# one card and forces diversity on the top slots via MMR.

# Cosine-similarity threshold for embedding-based clustering. Pairs of
# findings within the same competitor whose embedding cosine clears this
# bar are merged. 0.85 is conservative: voyage-3-lite tends to put true
# dupes (same event, different outlet) ≥0.88 and merely related stories
# in the 0.65–0.78 range, so this leans toward recall over silently
# collapsing distinct stories.
CLUSTER_COSINE_THRESHOLD: float = 0.85

# Title-Jaccard threshold used as the fallback when one or both findings
# in a pair lack a current-model embedding. 0.4 ≈ "3 of 7 content words
# overlap" — the knee between obvious dupes and merely related stories.
CLUSTER_JACCARD_THRESHOLD: float = 0.4

# Stopwords dropped before computing Jaccard. Kept intentionally tiny —
# content verbs ("launches", "hires", "raises") ARE the story and must
# stay in the token set.
CLUSTER_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "of", "to", "in", "on", "at",
    "and", "or", "for", "with", "by",
    "is", "are", "was", "were", "its", "it",
})

# MMR is applied to the top N cluster-leads; everything below is
# pure score-desc. 20 is one "screen" of attention; past that the user
# is deliberately digging and diversity there just confuses ordering.
MMR_WINDOW: int = 20

# Relevance / diversity tradeoff. 0.7 leans relevance but penalizes
# similarity enough that the top slots cover multiple competitors.
MMR_LAMBDA: float = 0.7

# Per-dimension similarity contribution. Sum clamped to [0, 1]. Same
# competitor + same signal_type = 0.8 — a strong enough "duplicate feel"
# pair that MMR will push the second one out of the adjacent slot.
MMR_SIM_WEIGHTS: dict[str, float] = {
    "competitor": 0.5,
    "signal_type": 0.3,
    "topic": 0.15,
    "source": 0.05,
}

# Stand-in scorer (used until spec 03's real scorer is wired in). Pure
# materiality + recency decay — preserves today's "new stuff first" feel
# while letting a high-materiality older item hold its place.
STANDIN_RECENCY_BOOST: float = 0.3
STANDIN_RECENCY_HALFLIFE_DAYS: float = 7.0

# Stand-in seen-decay (docs/ranker/07-seen-decay.md). Each prior view/open
# event for this finding subtracts PER_VIEW from the score, capped at
# MAX_VIEWS. Events inside the trailing EXCLUDE_MINUTES window don't count
# — that's the "current session" carve-out so within-session scrolling
# doesn't reshuffle the list. Zero per-view disables the decay without a
# code revert. Obsoleted by spec 03's `novelty_penalty` once the real
# scorer lands.
#
# `view` fires once per card per page-load (see app/static/signals.js), so
# N countable views ≈ N times the user has loaded the stream and seen this
# card.
STANDIN_SEEN_DECAY_PER_VIEW: float = 0.25
STANDIN_SEEN_DECAY_MAX_VIEWS: int = 3
STANDIN_SEEN_DECAY_EXCLUDE_MINUTES: int = 60


# ── Embedding / semantic ranking (spec 08) ───────────────────────────
# Voyage AI is the embedding provider; the adapter
# (app/adapters/voyage.py) reads VOYAGE_API_KEY at call time. When the
# key is unset, both finding-side embedding and centroid build silently
# no-op and the scorer's embedding term contributes 0.

# Model name passed to the Voyage SDK. Must agree with EMBEDDING_DIM
# below — a mismatch means the adapter rejects the response and logs
# `dim_mismatch`. Bumping this constant invalidates every existing
# Finding.embedding row (model recorded per-row); rerun the backfill
# script to refresh.
EMBEDDING_MODEL: str = "voyage-3-lite"
EMBEDDING_DIM: int = 512

# Safety cap before sending to Voyage. The model's real context is
# higher; this limit matches roughly the title + summary + content cap
# we already truncate to in summarize.py.
EMBEDDING_INPUT_CHAR_CAP: int = 8192

# Additive scorer term: `embedding_match = EMBEDDING_WEIGHT * cosine`.
# Picked slightly above the spec-03 materiality_boost (0.5) since
# semantic match is a richer signal than a global quality score, but
# bounded so a single term can't dominate the additive total.
EMBEDDING_WEIGHT: float = 0.6

# The centroid uses the same EVENT_WEIGHTS map the structured-dimension
# rollup uses — every signed event with an embedded finding contributes
# (positives pull toward, negatives push away). No separate event filter
# lives here; if EVENT_WEIGHTS says it counts, it counts toward the
# centroid too. See docs/ranker/08-semantic-ranking.md §Centroid.
