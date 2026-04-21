"""Debug + read API for the ranker preference profile
(docs/ranker/02-preference-rollup.md).

Users inspect their own profile via GET /me; POST /me/rebuild lets the
preference chat (spec 04) or debug UI force an immediate rollup.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..deps import get_current_user, get_db
from ..models import User
from ..ranker.preferences import load_profile
from ..ranker.rollup import rebuild_user_preferences

router = APIRouter(prefix="/api/preferences", tags=["preferences"])


# Per-spec-02: rebuild is rate-limited to 1 per 10s per user. Single-
# replica + single-process means in-memory state is authoritative.
# Move to a DB-backed counter if we ever scale out.
_REBUILD_MIN_INTERVAL_SEC: float = 10.0
_rebuild_lock = threading.Lock()
_last_rebuild_at: dict[int, float] = {}


_TOP_N_PER_DIMENSION: int = 10


@router.get("/me")
def get_my_preferences(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return the caller's materialized preference profile — the shape
    the debug UI + spec 04 chat read from. Negative weights included so
    consumers can surface 'what you dislike' alongside 'what you like'.
    """
    profile = load_profile(db, user.id)

    top: dict[str, list[dict]] = {}
    for dimension, entries in profile.vector.items():
        ranked = sorted(
            entries.values(),
            key=lambda e: abs(e.weight),
            reverse=True,
        )[:_TOP_N_PER_DIMENSION]
        top[dimension] = [
            {
                "key": e.key,
                "weight": e.weight,
                "raw_sum": e.raw_sum,
                "evidence_count": e.evidence_count,
                "positive_count": e.positive_count,
                "negative_count": e.negative_count,
                "last_event_at": e.last_event_at.isoformat(),
            }
            for e in ranked
        ]

    return {
        "user_id": profile.user_id,
        "cold_start": profile.cold_start,
        "event_count_30d": profile.event_count_30d,
        "last_computed_at": (
            profile.last_computed_at.isoformat() if profile.last_computed_at else None
        ),
        "taste_doc": profile.taste_doc,
        "schema_version": profile.schema_version,
        "top": top,
    }


@router.post("/me/rebuild")
def rebuild_my_preferences(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Force an inline rollup for the calling user. Preference chat
    uses this after committing a taste-doc change so the UI reflects
    the new state immediately.

    Rate-limited to 1 per 10s per user — the rollup scans up to 180
    days of events and writes N dimension rows, so a retry loop would
    thrash the DB. Returns 429 Too Many Requests when throttled.
    """
    now = time.monotonic()
    with _rebuild_lock:
        last = _last_rebuild_at.get(user.id, 0.0)
        if now - last < _REBUILD_MIN_INTERVAL_SEC:
            retry_after = int(_REBUILD_MIN_INTERVAL_SEC - (now - last)) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"rebuild throttled; retry in {retry_after}s",
                headers={"Retry-After": str(retry_after)},
            )
        _last_rebuild_at[user.id] = now

    summary = rebuild_user_preferences(db, user.id)
    return {
        "user_id": user.id,
        "events_considered": summary["events_considered"],
        "keys_written": summary["keys_written"],
        "event_count_30d": summary["event_count_30d"],
        "last_computed_at": datetime.utcnow().isoformat(),
    }
