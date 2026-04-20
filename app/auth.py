"""Password hashing + server-side session helpers.

Kept deliberately small: the `get_current_user` dependency in app/deps.py is
the single place these helpers get wired into the request lifecycle. Routes
that need auth never import from here directly — they depend on
`get_current_user` / `require_role` instead, matching the swap-point
discipline set in the stack notes.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import bcrypt
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from .models import AuthSession, User


SESSION_COOKIE = "cw_session"
SESSION_TTL = timedelta(days=30)
# Sliding refresh: if a session is within this window of expiry when it's
# used, push expires_at out by a full TTL. Keeps active users logged in
# without rotating tokens on every request.
SESSION_REFRESH_WINDOW = timedelta(days=7)

# bcrypt truncates input at 72 bytes. Enforced here so we never silently
# ignore trailing chars — callers get a length cap in the UI instead.
_BCRYPT_MAX_BYTES = 72


class AuthenticationRequired(HTTPException):
    """Raised by get_current_user when the request has no valid session.
    A global handler in main.py turns this into a 302/HX-Redirect/401 depending
    on the request shape."""

    def __init__(self, detail: str = "authentication required"):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _prepare(plain: str) -> bytes:
    # Encode to bytes and cap at 72 bytes (bcrypt's hard limit). Slicing on
    # bytes rather than chars avoids multibyte-boundary corruption for
    # non-ASCII passwords.
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_prepare(plain), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_prepare(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _new_token() -> str:
    # 32 bytes base64url → 43 chars, fits in String(64).
    return secrets.token_urlsafe(32)


def create_session(
    db: Session,
    user: User,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
) -> AuthSession:
    now = datetime.utcnow()
    sess = AuthSession(
        token=_new_token(),
        user_id=user.id,
        created_at=now,
        expires_at=now + SESSION_TTL,
        last_seen_at=now,
        user_agent=(user_agent or "")[:512] or None,
        ip=(ip or "")[:64] or None,
    )
    db.add(sess)
    user.last_login_at = now
    db.commit()
    db.refresh(sess)
    return sess


def lookup_session(db: Session, token: str) -> tuple[AuthSession, User] | None:
    """Return (session, user) if the token maps to a live session attached to
    an active user. Touches last_seen_at and slides expires_at when the
    session is inside the refresh window. Expired rows are swept so the table
    doesn't grow forever, but only opportunistically — no background job."""
    if not token:
        return None
    sess = db.get(AuthSession, token)
    if not sess:
        return None
    now = datetime.utcnow()
    if sess.expires_at <= now:
        db.delete(sess)
        db.commit()
        return None
    user = db.get(User, sess.user_id)
    if not user or not user.is_active:
        db.delete(sess)
        db.commit()
        return None

    sess.last_seen_at = now
    if sess.expires_at - now < SESSION_REFRESH_WINDOW:
        sess.expires_at = now + SESSION_TTL
    db.commit()
    return sess, user


def delete_session(db: Session, token: str) -> None:
    sess = db.get(AuthSession, token)
    if sess:
        db.delete(sess)
        db.commit()


def has_any_real_user(db: Session) -> bool:
    """First-run check: True when at least one user with a password exists.
    Drives the /setup vs /login landing decision."""
    return db.query(User).filter(User.password_hash.isnot(None)).count() > 0
