"""Preference rollup — turn the user_signal_events log into a per-user
preference vector (docs/ranker/02-preference-rollup.md).

Pure arithmetic, no LLM. Every run is deterministic for the same input
and idempotent (truncate-and-rewrite per user). Dropping the vector
table is safe: the next call reconstructs it from events.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy.orm import Session

from ..models import Finding, UserPreferenceProfile, UserPreferenceVector, UserSignalEvent
from . import config as rcfg


# Match spec 01 retention so we never decay events the prune job is
# about to delete. At 180 days with HALF_LIFE=30 the contribution is
# ~1.5% of base — a long tail of noise not worth including.
LOOKBACK_DAYS: int = 180


class _DimKey(NamedTuple):
    dimension: str
    key: str


class _Accumulator:
    """Per-user rollup state. Dict-of-dicts keyed by (dim, key) → stats."""

    __slots__ = ("raw_sum", "evidence", "positive", "negative", "last_at")

    def __init__(self) -> None:
        self.raw_sum: dict[_DimKey, float] = defaultdict(float)
        self.evidence: dict[_DimKey, int] = defaultdict(int)
        self.positive: dict[_DimKey, int] = defaultdict(int)
        self.negative: dict[_DimKey, int] = defaultdict(int)
        self.last_at: dict[_DimKey, datetime] = {}

    def contribute(self, dk: _DimKey, decayed: float, ts: datetime) -> None:
        if decayed == 0.0:
            # Still counts as evidence (the user interacted with a
            # finding touching this key) but doesn't move the sum. We
            # record it so the ranker knows coverage exists even for
            # zero-weighted event types like `view`.
            self.evidence[dk] += 1
        else:
            self.raw_sum[dk] += decayed
            self.evidence[dk] += 1
            if decayed > 0:
                self.positive[dk] += 1
            else:
                self.negative[dk] += 1
        prev = self.last_at.get(dk)
        if prev is None or ts > prev:
            self.last_at[dk] = ts


def _event_base_weight(event_type: str, dwell_ms: float | None) -> float:
    """Map one event to its signed base weight before decay. `dwell` is
    special — bucket-dependent; everything else is a dict lookup."""
    if event_type == "dwell":
        bucket = rcfg.dwell_bucket(dwell_ms)
        if bucket is None:
            return 0.0
        return rcfg.DWELL_WEIGHTS.get(bucket, 0.0)
    return rcfg.EVENT_WEIGHTS.get(event_type, 0.0)


def _finding_dim_keys(finding: Finding) -> list[_DimKey]:
    """Extract the (dimension, key) pairs this finding contributes to.
    Skips nulls and empty strings so we don't accumulate garbage keys."""
    out: list[_DimKey] = []
    for dim, attr in rcfg.FINDING_DIMENSIONS:
        val = getattr(finding, attr, None)
        if val is None:
            continue
        if isinstance(val, str):
            v = val.strip()
            if not v:
                continue
            # Cap key length to match the DB column; pathological LLM
            # topic output shouldn't crash the rollup.
            out.append(_DimKey(dim, v[:128]))
    return out


def rebuild_user_preferences(
    db: Session,
    user_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Rebuild one user's preference vector from their event log.

    Single transaction:
      1. Stream events for user in the last LOOKBACK_DAYS.
      2. Join each to its Finding to pull the five structured dimensions.
      3. Decay the base weight by age and accumulate per (dim, key).
      4. Truncate the user's vector rows and insert the fresh ones.
      5. Upsert profile metadata (event_count_30d, cold_start, timestamp).

    Returns a small summary dict for logging/tests:
      {"events_considered", "keys_written", "event_count_30d"}

    Caller owns DB session; commit happens here so the rebuild is atomic
    even when called inline from a request handler.
    """
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    # Pull events + joined findings in one go. Left join: events without
    # a finding (chat_pref_update, orphaned after finding delete) still
    # count toward event_count_30d but contribute no dimension weight.
    rows = (
        db.query(UserSignalEvent, Finding)
        .outerjoin(Finding, Finding.id == UserSignalEvent.finding_id)
        .filter(
            UserSignalEvent.user_id == user_id,
            UserSignalEvent.ts >= cutoff,
        )
        .all()
    )

    acc = _Accumulator()
    events_considered = 0
    cutoff_30d = now - timedelta(days=30)
    event_count_30d = 0
    ln2 = math.log(2.0)

    for event, finding in rows:
        events_considered += 1
        if event.ts >= cutoff_30d:
            event_count_30d += 1

        if finding is None:
            continue  # no dimensions to credit

        base = _event_base_weight(event.event_type, event.value)
        if base == 0.0 and event.event_type not in ("view", "shown", "dwell"):
            # Zero-weight event with no coverage value — skip entirely.
            # (view/shown/dwell still register coverage.)
            continue

        age_days = max(0.0, (now - event.ts).total_seconds() / 86400.0)
        decayed = base * math.exp(-ln2 * age_days / rcfg.HALF_LIFE_DAYS)

        for dk in _finding_dim_keys(finding):
            acc.contribute(dk, decayed, event.ts)

    # Onboarding seed has special dimension routing — meta.topics and
    # meta.competitors are arrays of keys to credit directly, no Finding
    # required. We handle it in a second pass for clarity.
    seed_base = rcfg.EVENT_WEIGHTS.get("onboarding_seed", 0.0)
    if seed_base != 0.0:
        for event, _finding in rows:
            if event.event_type != "onboarding_seed":
                continue
            meta = event.meta or {}
            age_days = max(0.0, (now - event.ts).total_seconds() / 86400.0)
            decayed = seed_base * math.exp(-ln2 * age_days / rcfg.HALF_LIFE_DAYS)
            for key in meta.get("competitors", []) or []:
                if isinstance(key, str) and key.strip():
                    acc.contribute(_DimKey("competitor", key.strip()[:128]),
                                   decayed, event.ts)
            for key in meta.get("topics", []) or []:
                if isinstance(key, str) and key.strip():
                    acc.contribute(_DimKey("topic", key.strip()[:128]),
                                   decayed, event.ts)

    # ── Write ────────────────────────────────────────────────────
    # Truncate-and-rewrite is atomic within one commit. Readers mid-rollup
    # either see the old state or the new state, never a partial mix.
    db.query(UserPreferenceVector).filter_by(user_id=user_id).delete(
        synchronize_session=False
    )

    # Only write rows with evidence. Zero-sum rows with no evidence would
    # be noise; the absence of a row means "no signal either way".
    keys_written = 0
    for dk, count in acc.evidence.items():
        if count == 0:
            continue
        raw = acc.raw_sum[dk]
        weight = math.tanh(raw)
        last_at = acc.last_at[dk]
        db.add(UserPreferenceVector(
            user_id=user_id,
            dimension=dk.dimension,
            key=dk.key,
            weight=weight,
            raw_sum=raw,
            evidence_count=count,
            positive_count=acc.positive[dk],
            negative_count=acc.negative[dk],
            last_event_at=last_at,
        ))
        keys_written += 1

    profile = db.get(UserPreferenceProfile, user_id)
    cold = event_count_30d < rcfg.COLD_START_THRESHOLD
    if profile is None:
        db.add(UserPreferenceProfile(
            user_id=user_id,
            cold_start=cold,
            event_count_30d=event_count_30d,
            last_computed_at=now,
            schema_version=rcfg.SCHEMA_VERSION,
        ))
    else:
        profile.cold_start = cold
        profile.event_count_30d = event_count_30d
        profile.last_computed_at = now
        profile.schema_version = rcfg.SCHEMA_VERSION
        # taste_doc is deliberately untouched — spec 04 owns it.

    db.commit()

    return {
        "events_considered": events_considered,
        "keys_written": keys_written,
        "event_count_30d": event_count_30d,
    }
