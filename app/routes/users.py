"""Admin user management: list existing users, invite new ones, toggle
active, reset a password. Intentionally narrow — no email invites, no
self-service signup. Admin creates the account + password and hands it to
the user out-of-band."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import hash_password
from ..deps import get_db, require_role
from ..models import User


router = APIRouter(tags=["users"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

ROLES = ["admin", "analyst", "viewer"]


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    rows = db.query(User).order_by(User.is_active.desc(), User.email).all()
    return templates.TemplateResponse(request, "admin_users.html", {
        "user": user, "users": rows, "roles": ROLES, "error": None, "message": None,
    })


@router.post("/admin/users", response_class=HTMLResponse)
def admin_users_create(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    role: str = Form("viewer"),
    password: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    email_norm = (email or "").strip().lower()
    name_norm = (name or "").strip()
    role_norm = role if role in ROLES else "viewer"
    err: str | None = None
    if not email_norm or "@" not in email_norm:
        err = "Enter a valid email."
    elif not name_norm:
        err = "Name is required."
    elif len(password) < 8:
        err = "Password must be at least 8 characters."

    if not err:
        existing = db.query(User).filter(User.email == email_norm).first()
        if existing and existing.password_hash:
            err = "A user with that email already exists."
        elif existing:
            existing.name = name_norm
            existing.role = role_norm
            existing.is_active = True
            existing.password_hash = hash_password(password)
        else:
            db.add(User(
                email=email_norm, name=name_norm, role=role_norm,
                password_hash=hash_password(password), is_active=True,
            ))
        db.commit()

    rows = db.query(User).order_by(User.is_active.desc(), User.email).all()
    return templates.TemplateResponse(request, "admin_users.html", {
        "user": user, "users": rows, "roles": ROLES,
        "error": err,
        "message": None if err else f"Created {email_norm}.",
    }, status_code=400 if err else 200)


@router.post("/admin/users/{user_id}/deactivate")
def admin_users_deactivate(
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404)
    if target.id == user.id:
        raise HTTPException(400, "can't deactivate yourself")
    target.is_active = False
    # Revoke their live sessions too, so the flip is immediate.
    from ..models import AuthSession
    db.query(AuthSession).filter(AuthSession.user_id == target.id).delete()
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/activate")
def admin_users_activate(
    user_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_role("admin")),
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404)
    target.is_active = True
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/reset_password")
def admin_users_reset_password(
    user_id: int,
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404)
    if len(password) < 8:
        rows = db.query(User).order_by(User.is_active.desc(), User.email).all()
        return templates.TemplateResponse(request, "admin_users.html", {
            "user": user, "users": rows, "roles": ROLES,
            "error": "Password must be at least 8 characters.", "message": None,
        }, status_code=400)
    target.password_hash = hash_password(password)
    # Kill all their sessions so they're forced to log back in.
    from ..models import AuthSession
    db.query(AuthSession).filter(AuthSession.user_id == target.id).delete()
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)
