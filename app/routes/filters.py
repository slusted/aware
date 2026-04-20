from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..deps import get_db, get_current_user
from ..models import SavedFilter, User
from ..schemas import SavedFilterIn, SavedFilterOut

router = APIRouter(prefix="/api/filters", tags=["filters"])


@router.get("", response_model=list[SavedFilterOut])
def list_filters(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return the user's own filters + every team-shared filter (owner_id NULL).
    Newest first; the UI puts them in a dropdown."""
    return (
        db.query(SavedFilter)
        .filter(or_(SavedFilter.owner_id == user.id, SavedFilter.owner_id.is_(None)))
        .order_by(SavedFilter.created_at.desc())
        .all()
    )


@router.post("", response_model=SavedFilterOut)
def create_filter(
    body: SavedFilterIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.visibility not in ("private", "team"):
        raise HTTPException(400, "visibility must be 'private' or 'team'")
    row = SavedFilter(
        # team-visible filters have no owner so every viewer sees them equally
        owner_id=None if body.visibility == "team" else user.id,
        name=body.name.strip(),
        spec=body.spec or {},
        visibility=body.visibility,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/{filter_id}")
def delete_filter(
    filter_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.get(SavedFilter, filter_id)
    if not row:
        raise HTTPException(404, "filter not found")
    # Team filters are admin-only to delete so no-one can quietly remove
    # a saved view the whole team relies on.
    if row.owner_id is None and user.role != "admin":
        raise HTTPException(403, "only admins can delete team filters")
    if row.owner_id and row.owner_id != user.id:
        raise HTTPException(403, "not your filter")
    db.delete(row)
    db.commit()
    return {"ok": True}
