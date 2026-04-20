"""Login / logout / first-run setup.

Keeps all auth UI on a bare layout (no sidebar, no status polling) so the
pages stay renderable before a session exists. Also exposes a JSON /api/auth
trio so API clients / tests can authenticate the same way as the browser.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    create_session,
    delete_session,
    has_any_real_user,
    hash_password,
    verify_password,
)
from ..deps import get_db
from ..models import User


router = APIRouter(tags=["auth"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _cookie_kwargs(request: Request) -> dict:
    # Secure cookies on HTTPS, plain on local dev. Lax is the right default
    # for session cookies behind form posts — we don't need Strict and None
    # would demand Secure unconditionally.
    return dict(
        key=SESSION_COOKIE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
        max_age=int(SESSION_TTL.total_seconds()),
    )


def _safe_next(raw: str | None) -> str:
    """Only honour same-site redirects. Anything odd → /."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    # Avoid bouncing back to auth pages.
    if raw.startswith(("/login", "/logout", "/setup")):
        return "/"
    return raw


# ─── Login ────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    # If nobody's registered yet, push straight to setup so the first admin
    # exists before anyone tries to sign in.
    if not has_any_real_user(db):
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next),
        "error": None,
    })


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    email_norm = (email or "").strip().lower()
    user = db.query(User).filter(User.email == email_norm).first()
    # Same generic message for "no such user" and "wrong password" — don't
    # leak which side failed.
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe_next(next), "error": "Invalid email or password.", "email": email_norm},
            status_code=400,
        )

    sess = create_session(
        db, user,
        user_agent=request.headers.get("user-agent"),
        ip=(request.client.host if request.client else None),
    )
    target = _safe_next(next)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(value=sess.token, **_cookie_kwargs(request))
    return resp


# ─── Logout ───────────────────────────────────────────────────────────────

@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        delete_session(db, token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ─── First-run setup ──────────────────────────────────────────────────────
# Only reachable until the first admin is created. After that the route
# hard-redirects to /login — the only way to add further users is via the
# admin UI once you're signed in.

@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)):
    if has_any_real_user(db):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@router.post("/setup", response_class=HTMLResponse)
def setup_submit(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if has_any_real_user(db):
        return RedirectResponse("/login", status_code=303)

    email_norm = (email or "").strip().lower()
    name_norm = (name or "").strip()
    err: str | None = None
    if not email_norm or "@" not in email_norm:
        err = "Enter a valid email."
    elif not name_norm:
        err = "Name is required."
    elif len(password) < 8:
        err = "Password must be at least 8 characters."
    elif password != password_confirm:
        err = "Passwords don't match."
    if err:
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": err, "email": email_norm, "name": name_norm},
            status_code=400,
        )

    # If a stub row already exists with this email (e.g. legacy admin@local
    # upgrading itself), reuse it so the FKs stay intact. Otherwise create
    # a fresh row.
    existing = db.query(User).filter(User.email == email_norm).first()
    if existing:
        existing.name = name_norm
        existing.role = "admin"
        existing.is_active = True
        existing.password_hash = hash_password(password)
        user = existing
    else:
        user = User(
            email=email_norm, name=name_norm, role="admin",
            password_hash=hash_password(password), is_active=True,
        )
        db.add(user)
    db.commit()
    db.refresh(user)

    sess = create_session(
        db, user,
        user_agent=request.headers.get("user-agent"),
        ip=(request.client.host if request.client else None),
    )
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(value=sess.token, **_cookie_kwargs(request))
    return resp
