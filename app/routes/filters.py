import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..deps import get_db, get_current_user
from ..models import SavedFilter, User
from ..schemas import SavedFilterIn, SavedFilterOut

router = APIRouter(prefix="/api/filters", tags=["filters"])


def _share_url(request: Request, token: str) -> str:
    """Build the absolute URL a recipient would paste into their browser.

    `request.base_url` already includes scheme + host + (optional) port and
    a trailing slash, so we just append `p/{token}`. Honours the
    X-Forwarded-* headers Railway sets so the URL renders as https in prod
    instead of http://internal:8080.
    """
    return f"{str(request.base_url).rstrip('/')}/p/{token}"


def _check_share_owner(sf: SavedFilter, user: User) -> None:
    """Ownership gate for any share operation. Mirrors delete_filter:
    own private filters = owner; team filters = admin only."""
    if sf.owner_id is None and user.role != "admin":
        raise HTTPException(403, "only admins can share team filters")
    if sf.owner_id and sf.owner_id != user.id:
        raise HTTPException(403, "not your filter")


def _check_mintable(sf: SavedFilter) -> None:
    """Mint-only check: refuse pinned-only specs because pins live on the
    viewer's user_signal_events and the public render has no viewer.
    Revoke deliberately skips this so a filter that was edited to be
    pinned-only after sharing can still be revoked."""
    if (sf.spec or {}).get("pinned_only"):
        raise HTTPException(400, "pinned-only filters can't be shared (pins are per-user)")


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


# ---- Public share link (docs/stream/01-public-share-link.md) -----------


@router.get("/{filter_id}/share")
def get_share(
    filter_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Read the current share-link state without fetching the whole filter
    list. Returns {public_token, share_url} (both null when not shared)."""
    sf = db.get(SavedFilter, filter_id)
    if not sf:
        raise HTTPException(404, "filter not found")
    # Read is gated the same as mint so a non-admin can't probe team-filter
    # tokens by walking ids. Mirrors delete_filter's permission shape.
    if sf.owner_id is None and user.role != "admin":
        raise HTTPException(403, "only admins can view team-filter share state")
    if sf.owner_id and sf.owner_id != user.id:
        raise HTTPException(403, "not your filter")
    return {
        "public_token": sf.public_token,
        "share_url": _share_url(request, sf.public_token) if sf.public_token else None,
        "created_at": sf.public_token_created_at,
    }


@router.post("/{filter_id}/share")
def mint_share(
    filter_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Mint or rotate the share token. Idempotent in intent: every call
    produces a fresh token and overwrites any existing one (rotation is
    the same gesture as initial share)."""
    sf = db.get(SavedFilter, filter_id)
    if not sf:
        raise HTTPException(404, "filter not found")
    _check_share_owner(sf, user)
    _check_mintable(sf)
    # 256 bits of entropy. One retry on the (vanishingly unlikely) collision
    # so the unique index becomes a hard failure mode rather than a silent
    # overwrite of someone else's token.
    for _ in range(2):
        sf.public_token = secrets.token_urlsafe(32)
        sf.public_token_created_at = datetime.utcnow()
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            continue
    else:
        raise HTTPException(500, "failed to mint a unique share token")
    return {
        "public_token": sf.public_token,
        "share_url": _share_url(request, sf.public_token),
        "created_at": sf.public_token_created_at,
    }


@router.delete("/{filter_id}/share")
def revoke_share(
    filter_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Revoke the share link. Sets public_token to NULL; the URL 404s
    immediately. Doesn't touch any other filter state."""
    sf = db.get(SavedFilter, filter_id)
    if not sf:
        raise HTTPException(404, "filter not found")
    _check_share_owner(sf, user)
    sf.public_token = None
    sf.public_token_created_at = None
    db.commit()
    return {"ok": True}
