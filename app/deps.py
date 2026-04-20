from typing import Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .auth import AuthenticationRequired, SESSION_COOKIE, lookup_session
from .db import SessionLocal
from .models import User


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Resolve the logged-in user from the session cookie.

    Raises AuthenticationRequired when the request has no valid session —
    main.py's handler turns that into a login redirect for HTML requests, an
    HX-Redirect for HTMX partials, and a 401 JSON body for API calls. Route
    signatures elsewhere in the app don't change; this is still just
    `user = Depends(get_current_user)`.
    """
    token = request.cookies.get(SESSION_COOKIE, "")
    hit = lookup_session(db, token)
    if not hit:
        raise AuthenticationRequired()
    _, user = hit
    return user


def require_role(*allowed: str):
    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(403, f"requires role in {allowed}")
        return user

    return _check
