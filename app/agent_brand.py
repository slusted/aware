"""App-wide configurable brand for the in-product agent.

Surfaces in three template sites today:
  - sidebar Agent launcher (base.html, .nav-agent)
  - floating chat launcher (base.html, .chat-launcher)
  - chat-drawer picker header (_chat_drawer_picker.html)

Two settings:
  - name: short string (default "Flo")
  - avatar_version: int counter; bumped each upload so the served URL
    cache-busts. The bytes live at DATA_DIR/uploads/agent.png. When no
    upload exists yet, the route falls back to the bundled default at
    app/static/florian.png.

Templates read via a Jinja global `agent_brand` (LazyBrand instance) —
`{{ agent_brand.name }}` and `{{ agent_brand.avatar_url }}`. The cache
is primed on app startup and refreshed on each write, so renders never
hit the DB.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import AgentBrand


DEFAULT_NAME = "Flo"
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
UPLOADS_DIR = DATA_DIR / "uploads"
AVATAR_FILENAME = "agent.png"
AVATAR_PATH = UPLOADS_DIR / AVATAR_FILENAME

# Bundled fallback served when no user upload exists. Lives in /static so
# the existing static mount handles it without a custom route.
DEFAULT_AVATAR_URL = "/static/florian.png?v=flo-1"

# Pillow processing knobs. 512 is plenty for a 56px launcher and a 1024px
# settings preview; bigger inflates the persisted file with no visible win.
AVATAR_PIXELS = 512
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB pre-processing cap.
ACCEPTED_MIME_PREFIXES = ("image/png", "image/jpeg", "image/webp")


# ── Cache ────────────────────────────────────────────────────
# Single dict refreshed from DB on startup and on every write. Reads
# happen on every page render, so the DB round-trip would be wasteful.

_cache: dict | None = None


def _row_to_dict(row: AgentBrand) -> dict:
    return {"name": row.name or DEFAULT_NAME, "avatar_version": int(row.avatar_version or 0)}


def _load(db: Session) -> dict:
    row = db.query(AgentBrand).filter(AgentBrand.id == 1).first()
    if row is None:
        # Migration seeds id=1, but be defensive — a wiped DB or a fresh
        # create_all may skip the seed. Insert here so first read on any
        # environment works.
        row = AgentBrand(id=1, name=DEFAULT_NAME, avatar_version=0)
        db.add(row)
        db.commit()
        db.refresh(row)
    return _row_to_dict(row)


def _ensure_loaded() -> dict:
    global _cache
    if _cache is None:
        db = SessionLocal()
        try:
            _cache = _load(db)
        finally:
            db.close()
    return _cache


def prime() -> None:
    """Call once at app startup so the first render doesn't pay a DB hit."""
    _ensure_loaded()


def refresh(db: Session) -> dict:
    """Reload from DB — call after any write."""
    global _cache
    _cache = _load(db)
    return _cache


# ── Public read API ───────────────────────────────────────────

def get_name() -> str:
    return _ensure_loaded()["name"]


def get_avatar_url() -> str:
    """URL for the current avatar with version-based cache-busting.

    Always returns the served route (`/agent-avatar`) — the route handles
    the upload-vs-default fallback so callers don't need to know which is
    in play. Embedding the version in the URL means browsers re-fetch
    immediately after an upload."""
    v = _ensure_loaded()["avatar_version"]
    return f"/agent-avatar?v={v}"


# ── Public write API ──────────────────────────────────────────

def update_name(db: Session, new_name: str) -> dict:
    name = (new_name or "").strip()
    if not name:
        raise ValueError("Name cannot be empty")
    if len(name) > 64:
        raise ValueError("Name must be 64 characters or fewer")
    row = db.query(AgentBrand).filter(AgentBrand.id == 1).first()
    if row is None:
        row = AgentBrand(id=1)
        db.add(row)
    row.name = name
    db.commit()
    return refresh(db)


def save_uploaded_avatar(db: Session, raw_bytes: bytes) -> dict:
    """Validate, normalise, persist. Returns the refreshed cache dict.

    Pillow handles format detection, EXIF rotation, and the centre-crop
    + resize. Output is always PNG so transparency in source images is
    preserved (the launcher renders the avatar inside a circular clip,
    so any padding around the subject reads as a clean ring)."""
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Image is too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    if len(raw_bytes) == 0:
        raise ValueError("Empty file")

    # Local import so a missing Pillow fails the upload but doesn't break
    # the rest of the app at import time.
    from io import BytesIO
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        im = Image.open(BytesIO(raw_bytes))
        im.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError(f"Could not read image: {e}")

    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")

    # Centre-crop to a square, then resize. ImageOps.fit does both in one
    # call and uses LANCZOS by default in modern Pillow.
    im = ImageOps.fit(im, (AVATAR_PIXELS, AVATAR_PIXELS), method=Image.Resampling.LANCZOS)

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write: write to a temp path next to the target, then
    # rename. Avoids serving a half-written file if the process crashes
    # mid-save.
    tmp_path = AVATAR_PATH.with_suffix(".png.tmp")
    im.save(tmp_path, format="PNG", optimize=True)
    tmp_path.replace(AVATAR_PATH)

    row = db.query(AgentBrand).filter(AgentBrand.id == 1).first()
    if row is None:
        row = AgentBrand(id=1, name=DEFAULT_NAME)
        db.add(row)
    row.avatar_version = int(row.avatar_version or 0) + 1
    db.commit()
    return refresh(db)


def avatar_disk_path() -> Optional[Path]:
    """Local path of the user-uploaded avatar, or None if none exists."""
    return AVATAR_PATH if AVATAR_PATH.exists() else None


# ── Lazy proxy for Jinja globals ──────────────────────────────
# Templates use `{{ agent_brand.name }}` / `{{ agent_brand.avatar_url }}`
# — clean attribute access, no parens, but reads the live cache so an
# update mid-process is reflected immediately on the next render.

class _LazyBrand:
    @property
    def name(self) -> str:
        return get_name()

    @property
    def avatar_url(self) -> str:
        return get_avatar_url()


lazy_brand = _LazyBrand()


def register_template_globals(templates) -> None:
    """Wire `agent_brand` into a Jinja2Templates env.

    Each FastAPI router that renders templates constructs its own
    Jinja2Templates instance (chat.py, scenarios.py, schedules.py, …).
    Each instance gets its own Jinja Environment, so the global has to
    be set on every one of them — base.html now reads
    `{{ agent_brand.name }}` / `{{ agent_brand.avatar_url }}` from the
    sidebar, which is rendered on every authenticated page including
    /chat, /scenarios, etc."""
    templates.env.globals["agent_brand"] = lazy_brand
