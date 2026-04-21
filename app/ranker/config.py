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
