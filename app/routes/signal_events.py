"""Client-facing endpoints for appending UserSignalEvent rows. This is the
input side of the ranker's signal log (docs/ranker/01-signal-log.md).

Server-emitted events (pin/dismiss/snooze/shown) bypass this module —
they're written directly from their originating routes or the stream
render path. These endpoints exist for the client-postable subset:
implicit events (view, dwell, open) and explicit ratings.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..deps import get_db, get_current_user
from ..models import Finding, SignalView, User, UserSignalEvent
from ..ranker import config as ranker_config
from ..ranker.events import EventValidationError, validate_event
from ..scheduler import schedule_incremental_rebuild
from ..schemas import SignalEventBatchIn, SignalEventIn

router = APIRouter(prefix="/api/signals", tags=["signal-events"])


# Spec 01: reject a `view` for the same (user_id, finding_id) pair within
# this window. Prevents scroll-thrash inflation without losing legitimate
# re-visits that happen across sessions.
VIEW_DEDUP_WINDOW = timedelta(minutes=5)

# Spec 01: batch endpoint ceiling. Enough for a full stream page of `shown`
# events plus ratings, small enough to cap request size.
MAX_BATCH_SIZE = 100


def emit_shown_events(
    user_id: int,
    finding_ids: list[int],
    *,
    filter_id: int | None = None,
    source: str = "stream",
) -> None:
    """Background-task helper: bulk-insert a `shown` row per finding.

    Opens its own DB session — the request-scoped session from FastAPI's
    Depends(get_db) is closed before BackgroundTasks run, so we can't
    reuse it. Fire-and-forget: exceptions are swallowed so a stream
    render can't crash on logging failure. Keeps position in meta so the
    rollup can weight "top of feed" differently later if we choose.
    """
    if not finding_ids:
        return
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        rows = [
            UserSignalEvent(
                user_id=user_id,
                finding_id=fid,
                event_type="shown",
                source=source,
                meta=(
                    {"position": idx, "filter_id": filter_id}
                    if filter_id is not None
                    else {"position": idx}
                ),
                ts=now,
            )
            for idx, fid in enumerate(finding_ids)
        ]
        db.add_all(rows)
        db.commit()
    except Exception as e:
        # Deliberately non-fatal — the stream has already rendered.
        print(f"[signal_events] emit_shown failed: {e}", flush=True)
        db.rollback()
    finally:
        db.close()


def _is_view_duplicate(
    db: Session, user_id: int, finding_id: int | None, now: datetime
) -> bool:
    """True iff a `view` event already exists for this (user, finding) pair
    within VIEW_DEDUP_WINDOW. Only called for event_type='view'."""
    if finding_id is None:
        return False
    cutoff = now - VIEW_DEDUP_WINDOW
    return (
        db.query(UserSignalEvent.id)
        .filter(
            UserSignalEvent.user_id == user_id,
            UserSignalEvent.finding_id == finding_id,
            UserSignalEvent.event_type == "view",
            UserSignalEvent.ts >= cutoff,
        )
        .first()
        is not None
    )


def _validate_or_400(payload: SignalEventIn) -> None:
    """Run the closed-taxonomy validator and translate any failure into a
    clean 400. client_origin=True enforces the client-postable subset."""
    try:
        validate_event(
            event_type=payload.event_type,
            source=payload.source,
            finding_id=payload.finding_id,
            client_origin=True,
        )
    except EventValidationError as e:
        raise HTTPException(400, str(e)) from e


def _mark_seen_if_new(
    db: Session, user_id: int, finding_ids: set[int], now: datetime
) -> None:
    """Insert SignalView(state='seen') for any (user, finding) pair that
    doesn't already have a row. Never overwrites — pinned/dismissed/snoozed
    take precedence over a passive view. Absence of a row is the "new/unread"
    marker used by the stream UI.

    Called after a batch of `view` events is accepted so read-state tracks
    what the user has actually scrolled past for ≥500ms.
    """
    if not finding_ids:
        return
    existing = {
        row[0]
        for row in db.query(SignalView.finding_id)
        .filter(
            SignalView.user_id == user_id,
            SignalView.finding_id.in_(finding_ids),
        )
        .all()
    }
    to_create = finding_ids - existing
    if not to_create:
        return
    db.add_all(
        SignalView(
            user_id=user_id,
            finding_id=fid,
            state="seen",
            updated_at=now,
        )
        for fid in to_create
    )


def _check_findings_exist(db: Session, finding_ids: set[int]) -> None:
    """Raise 404 if any finding_id in the set doesn't exist. Single query
    regardless of batch size — keeps the batch endpoint cheap."""
    if not finding_ids:
        return
    found = {
        row[0]
        for row in db.query(Finding.id).filter(Finding.id.in_(finding_ids)).all()
    }
    missing = finding_ids - found
    if missing:
        # Surface just one to keep the error body small; the client has
        # the full list in its request.
        raise HTTPException(404, f"finding not found: {next(iter(missing))}")


@router.post("/event", status_code=status.HTTP_204_NO_CONTENT)
def post_event(
    payload: SignalEventIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Append a single user-signal event.

    Returns 204 on insert AND on silent de-dup reject — the client doesn't
    need to distinguish, and treating de-dup as an error would cause client
    JS to retry. 400 on taxonomy violation, 404 on missing finding_id.
    """
    _validate_or_400(payload)
    if payload.finding_id is not None:
        _check_findings_exist(db, {payload.finding_id})

    now = datetime.utcnow()
    if payload.event_type == "view" and _is_view_duplicate(
        db, user.id, payload.finding_id, now
    ):
        # Silent accept — client gets a 204 and moves on.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    db.add(
        UserSignalEvent(
            user_id=user.id,
            finding_id=payload.finding_id,
            event_type=payload.event_type,
            value=payload.value,
            source=payload.source,
            meta=payload.meta or {},
            ts=now,
        )
    )
    if payload.event_type == "view" and payload.finding_id is not None:
        _mark_seen_if_new(db, user.id, {payload.finding_id}, now)
    db.commit()

    # Explicit high-intent events (ratings, chat prefs) trigger an
    # incremental rollup so the ranker sees the signal within a minute
    # instead of waiting for the nightly sweep. Debounced inside the
    # scheduler helper — no-op if a rebuild is already pending.
    if payload.event_type in ranker_config.INCREMENTAL_TRIGGER_TYPES:
        schedule_incremental_rebuild(user.id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/events/batch", status_code=status.HTTP_204_NO_CONTENT)
def post_events_batch(
    payload: SignalEventBatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Append up to MAX_BATCH_SIZE events in a single transaction.

    Fail-fast validation: a single bad event (unknown type/source, missing
    finding, server-only type) rejects the whole batch with 400/404. The
    client is expected to send well-formed events — this endpoint is a bulk
    optimisation, not an error-tolerant ingestion path. `view` de-dup is
    applied per event inside the batch; de-duped rows are silently dropped
    (matches single-event behaviour).
    """
    events = payload.events
    if not events:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if len(events) > MAX_BATCH_SIZE:
        raise HTTPException(
            400, f"batch size {len(events)} exceeds max {MAX_BATCH_SIZE}"
        )

    # 1. Taxonomy validation for every event first — reject as a whole.
    for ev in events:
        _validate_or_400(ev)

    # 2. Existence check for every referenced finding_id, in one query.
    finding_ids = {ev.finding_id for ev in events if ev.finding_id is not None}
    _check_findings_exist(db, finding_ids)

    # 3. De-dup `view` events against the log. One query covers the whole
    # batch — avoids N round-trips when flushing on beforeunload.
    now = datetime.utcnow()
    cutoff = now - VIEW_DEDUP_WINDOW
    view_finding_ids = {
        ev.finding_id
        for ev in events
        if ev.event_type == "view" and ev.finding_id is not None
    }
    recent_views: set[int] = set()
    if view_finding_ids:
        recent_views = {
            row[0]
            for row in db.query(UserSignalEvent.finding_id)
            .filter(
                UserSignalEvent.user_id == user.id,
                UserSignalEvent.event_type == "view",
                UserSignalEvent.finding_id.in_(view_finding_ids),
                UserSignalEvent.ts >= cutoff,
            )
            .all()
        }

    # Also de-dup *within* the batch itself: two `view` events for the same
    # finding in one POST should produce at most one row.
    seen_view_pairs: set[int] = set()

    to_insert: list[UserSignalEvent] = []
    newly_viewed: set[int] = set()
    for ev in events:
        if ev.event_type == "view" and ev.finding_id is not None:
            if ev.finding_id in recent_views or ev.finding_id in seen_view_pairs:
                continue
            seen_view_pairs.add(ev.finding_id)
            newly_viewed.add(ev.finding_id)
        to_insert.append(
            UserSignalEvent(
                user_id=user.id,
                finding_id=ev.finding_id,
                event_type=ev.event_type,
                value=ev.value,
                source=ev.source,
                meta=ev.meta or {},
                ts=now,
            )
        )

    if to_insert:
        db.add_all(to_insert)
        _mark_seen_if_new(db, user.id, newly_viewed, now)
        db.commit()

    # Same incremental-rebuild trigger as the single-event endpoint: if
    # any high-intent event made it through the batch, nudge the rollup.
    # Once per batch is enough — the scheduler debounces by user.
    if any(ev.event_type in ranker_config.INCREMENTAL_TRIGGER_TYPES for ev in events):
        schedule_incremental_rebuild(user.id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
