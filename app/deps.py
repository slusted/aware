from typing import Generator
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session
from .db import SessionLocal
from .models import User


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(db: Session = Depends(get_db)) -> User:
    """Phase 1 stub: always returns the single admin user.
    When real auth lands, swap this dependency — endpoint signatures don't change.
    """
    user = db.query(User).filter(User.email == "admin@local").first()
    if not user:
        user = User(email="admin@local", name="Admin", role="admin")
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def require_role(*allowed: str):
    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(403, f"requires role in {allowed}")
        return user
    return _check
