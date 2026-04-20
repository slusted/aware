from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..deps import get_db, get_current_user
from ..models import Finding, SignalView, User
from ..schemas import FindingOut, SignalViewIn

router = APIRouter(prefix="/api/findings", tags=["findings"])


@router.get("", response_model=list[FindingOut])
def list_findings(
    competitor: str | None = None,
    signal_types: Annotated[list[str] | None, Query()] = None,
    min_materiality: float | None = None,
    since_days: int | None = None,
    exclude_dismissed: bool = True,
    exclude_snoozed: bool = True,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Stream query. Filters compose; all are optional.

    view_state on each result reflects SignalView rows for the calling user.
    A null state means 'new/unseen'. exclude_dismissed and exclude_snoozed
    are the default so the stream surfaces only actionable signals.
    """
    q = db.query(Finding)

    if competitor:
        q = q.filter(Finding.competitor == competitor)
    if signal_types:
        q = q.filter(Finding.signal_type.in_(signal_types))
    if min_materiality is not None:
        q = q.filter(Finding.materiality >= min_materiality)
    if since_days:
        cutoff = datetime.utcnow() - timedelta(days=since_days)
        q = q.filter(Finding.created_at >= cutoff)

    # View-state filters share an outer join on SignalView scoped to this user.
    if exclude_dismissed or exclude_snoozed:
        q = q.outerjoin(
            SignalView,
            and_(
                SignalView.finding_id == Finding.id,
                SignalView.user_id == user.id,
            ),
        )
        if exclude_dismissed:
            q = q.filter(
                or_(SignalView.state.is_(None), SignalView.state != "dismissed")
            )
        if exclude_snoozed:
            now = datetime.utcnow()
            q = q.filter(
                or_(
                    SignalView.state.is_(None),
                    SignalView.state != "snoozed",
                    SignalView.snoozed_until.is_(None),
                    SignalView.snoozed_until < now,
                )
            )

    findings = (
        q.order_by(Finding.created_at.desc()).offset(offset).limit(limit).all()
    )

    # Second query rather than threading the join into the SELECT: SQLAlchemy
    # can't cleanly return a Finding row + extra columns through a pydantic
    # response_model. Two queries hit the same indexes and are bounded by limit.
    views: dict[int, SignalView] = {}
    if findings:
        ids = [f.id for f in findings]
        for v in (
            db.query(SignalView)
            .filter(SignalView.user_id == user.id, SignalView.finding_id.in_(ids))
            .all()
        ):
            views[v.finding_id] = v

    out: list[FindingOut] = []
    for f in findings:
        v = views.get(f.id)
        out.append(
            FindingOut(
                id=f.id,
                competitor=f.competitor,
                source=f.source,
                topic=f.topic,
                title=f.title,
                url=f.url,
                content=f.content,
                created_at=f.created_at,
                signal_type=f.signal_type,
                materiality=f.materiality,
                published_at=f.published_at,
                search_provider=f.search_provider,
                score=f.score,
                view_state=v.state if v else None,
                snoozed_until=v.snoozed_until if v else None,
            )
        )
    return out


_ALLOWED_STATES = {"seen", "pinned", "dismissed", "snoozed"}


@router.post("/{finding_id}/view")
def upsert_view(
    finding_id: int,
    body: SignalViewIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Record a user's interaction with a signal.

    Upsert-by-(user_id, finding_id): first interaction inserts, subsequent
    ones overwrite state + snoozed_until. `snoozed_until` is required only
    when state='snoozed'; ignored otherwise.
    """
    if body.state not in _ALLOWED_STATES:
        raise HTTPException(
            400, f"state must be one of {sorted(_ALLOWED_STATES)}"
        )
    if body.state == "snoozed" and not body.snoozed_until:
        raise HTTPException(400, "snoozed_until is required when state=snoozed")

    if not db.get(Finding, finding_id):
        raise HTTPException(404, "finding not found")

    existing = (
        db.query(SignalView)
        .filter(
            SignalView.user_id == user.id,
            SignalView.finding_id == finding_id,
        )
        .first()
    )
    now = datetime.utcnow()
    if existing:
        existing.state = body.state
        existing.snoozed_until = body.snoozed_until if body.state == "snoozed" else None
        existing.updated_at = now
    else:
        db.add(
            SignalView(
                user_id=user.id,
                finding_id=finding_id,
                state=body.state,
                snoozed_until=body.snoozed_until if body.state == "snoozed" else None,
                updated_at=now,
            )
        )
    db.commit()
    return {"ok": True, "finding_id": finding_id, "state": body.state}
