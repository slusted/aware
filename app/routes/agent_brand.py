"""API for the configurable agent brand (name + avatar).

Reads happen via the Jinja global `agent_brand` (see app/agent_brand.py).
This module owns the writes and the avatar-serving endpoint.

Both endpoints are admin-only — agent identity is app-wide, not per-user,
so non-admins shouldn't be able to rename it or replace the picture.
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import agent_brand
from ..deps import get_db, require_role


router = APIRouter(tags=["agent_brand"])


@router.get("/agent-avatar")
def serve_agent_avatar():
    """Public — embedded in every authenticated page header. Falls back
    to the bundled /static/florian.png when no upload exists yet so the
    sidebar/launcher always have an image to show."""
    path = agent_brand.avatar_disk_path()
    if path is None:
        return RedirectResponse(agent_brand.DEFAULT_AVATAR_URL, status_code=307)
    # Long max-age is safe because callers append `?v={version}` to the
    # URL — bumping the version invalidates the cache instantly.
    return FileResponse(
        str(path),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/api/settings/agent")
async def update_agent_brand(
    name: str | None = Form(None),
    avatar: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    _=Depends(require_role("admin")),
):
    """Multipart form. Either field is optional, but at least one must
    be provided so a noop POST doesn't quietly succeed."""
    did_anything = False

    if name is not None and name.strip():
        try:
            agent_brand.update_name(db, name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        did_anything = True

    if avatar is not None and avatar.filename:
        if avatar.content_type and not any(
            avatar.content_type.startswith(p) for p in agent_brand.ACCEPTED_MIME_PREFIXES
        ):
            raise HTTPException(
                400,
                f"Unsupported file type {avatar.content_type!r} — use PNG, JPG, or WEBP",
            )
        raw = await avatar.read()
        try:
            agent_brand.save_uploaded_avatar(db, raw)
        except ValueError as e:
            raise HTTPException(400, str(e))
        did_anything = True

    if not did_anything:
        raise HTTPException(400, "Provide a new name or an avatar image")

    return {
        "ok": True,
        "name": agent_brand.get_name(),
        "avatar_url": agent_brand.get_avatar_url(),
    }
