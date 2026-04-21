"""Read-side interface for the preference vector. Spec 03 (ranker) will
consume this exclusively — it must not inline SQL against
user_preferences_vector or user_preference_profile.

Keeping the scorer behind this seam means we can swap the sparse-vector
backend for an embedding-aware one later without rewriting the ranker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import UserPreferenceProfile, UserPreferenceVector


@dataclass(frozen=True)
class DimensionEntry:
    """One weighted key within a dimension. What the ranker consumes."""
    key: str
    weight: float
    raw_sum: float
    evidence_count: int
    positive_count: int
    negative_count: int
    last_event_at: datetime


@dataclass
class UserProfile:
    """Complete preference profile for one user. `vector` is a nested
    dict: dimension → key → DimensionEntry. `cold_start` is the fast
    path the scorer reads before spending time on lookups."""
    user_id: int
    cold_start: bool
    event_count_30d: int
    last_computed_at: datetime | None
    taste_doc: str | None
    schema_version: int
    vector: dict[str, dict[str, DimensionEntry]] = field(default_factory=dict)

    def dimension_weight(self, dimension: str, key: str) -> float:
        """Signed weight for a single (dim, key). 0.0 when missing —
        the ranker should treat "no evidence" as neutral, not negative."""
        d = self.vector.get(dimension)
        if not d:
            return 0.0
        entry = d.get(key)
        return entry.weight if entry is not None else 0.0

    def dimension_entry(self, dimension: str, key: str) -> DimensionEntry | None:
        """Full entry for explainability (ranker reasons string)."""
        d = self.vector.get(dimension)
        return d.get(key) if d else None

    def top_keys(self, dimension: str, n: int = 10) -> list[tuple[str, float]]:
        """Top-N keys by |weight| within a dimension. Negatives included —
        they're useful context for 'why did this rank poorly'."""
        d = self.vector.get(dimension, {})
        ranked = sorted(d.items(), key=lambda kv: abs(kv[1].weight), reverse=True)
        return [(k, e.weight) for k, e in ranked[:n]]


def load_profile(db: Session, user_id: int) -> UserProfile:
    """Materialize the user's profile from both tables in two queries.

    Missing profile row → cold-start default (ranker falls back to
    recency). Missing vector rows → empty dict. The scorer is safe to
    call this for any user, including brand-new ones.
    """
    profile_row = db.get(UserPreferenceProfile, user_id)
    vector_rows = (
        db.query(UserPreferenceVector)
        .filter(UserPreferenceVector.user_id == user_id)
        .all()
    )

    vector: dict[str, dict[str, DimensionEntry]] = {}
    for row in vector_rows:
        bucket = vector.setdefault(row.dimension, {})
        bucket[row.key] = DimensionEntry(
            key=row.key,
            weight=row.weight,
            raw_sum=row.raw_sum,
            evidence_count=row.evidence_count,
            positive_count=row.positive_count,
            negative_count=row.negative_count,
            last_event_at=row.last_event_at,
        )

    if profile_row is None:
        # No rollup has ever run for this user — treat as cold.
        return UserProfile(
            user_id=user_id,
            cold_start=True,
            event_count_30d=0,
            last_computed_at=None,
            taste_doc=None,
            schema_version=0,
            vector=vector,
        )

    return UserProfile(
        user_id=user_id,
        cold_start=profile_row.cold_start,
        event_count_30d=profile_row.event_count_30d,
        last_computed_at=profile_row.last_computed_at,
        taste_doc=profile_row.taste_doc,
        schema_version=profile_row.schema_version,
        vector=vector,
    )
