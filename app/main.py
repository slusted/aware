import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import AuthenticationRequired

try:
    from dotenv import load_dotenv
    # ENV_PATH lets us point at a .env on a persistent volume (Railway/Render/Fly),
    # so UI-managed keys survive redeploys. Dashboard env vars still win.
    load_dotenv(os.environ.get("ENV_PATH") or ".env")
except ImportError:
    pass

from datetime import datetime

from .db import SessionLocal, engine
from . import scheduler, skills as skills_module, ui, usage, search_providers
from .models import Run, RunEvent
from .routes import status, competitors, runs, findings, reports, usage as usage_routes, skills as skills_routes, context as context_routes, providers as providers_routes, env_keys as env_keys_routes, filters as filters_routes, auth as auth_routes, users as users_routes


def _reap_orphan_runs() -> int:
    """Any run still marked 'running' at boot was killed mid-flight by a previous
    process. Flip it to error so the dashboard doesn't say 'Running…' forever."""
    db = SessionLocal()
    try:
        orphans = db.query(Run).filter(Run.status.in_(["running", "cancelling"])).all()
        for r in orphans:
            r.status = "error"
            r.error = "interrupted — process restarted before the run finished"
            r.finished_at = datetime.utcnow()
            db.add(RunEvent(run_id=r.id, level="warn",
                            message="run marked as error on startup (orphaned)"))
        db.commit()
        return len(orphans)
    finally:
        db.close()


def _migrate_schema():
    """Bring the DB up to head at startup.

    Earlier deploys let `Base.metadata.create_all()` own the schema, which
    only creates missing tables — it never adds new columns. When the auth
    columns landed in `l8f2a3b4c6d7`, that left existing Railway installs
    stuck on a stale `users` table with no `password_hash`, and the Procfile
    `release` step on Railway turned out not to fire. Running alembic here
    closes both gaps: fresh DBs get the full migration chain; legacy DBs
    get stamped at whatever revision their observable schema matches, then
    upgraded to head.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    if tables and "alembic_version" not in tables:
        # Legacy DB produced by create_all(); infer the stamp point from the
        # schema we can see. Only two branch points matter in practice: pre-
        # vs post-auth, since the auth migration is the first one that adds
        # columns rather than creating new tables.
        user_cols = {c["name"] for c in insp.get_columns("users")} if "users" in tables else set()
        stamp_at = "l8f2a3b4c6d7" if "password_hash" in user_cols else "k7e1f2a3b4c5"
        print(f"  [startup] stamping legacy DB at {stamp_at}")
        command.stamp(cfg, stamp_at)

    command.upgrade(cfg, "head")


def _seed_volume_config():
    """On a fresh persistent volume (Railway/Render/Fly), copy the repo's
    config.json to CONFIG_PATH if that target doesn't exist yet. Keeps the UI's
    live edits on the volume instead of the ephemeral app filesystem."""
    target = os.environ.get("CONFIG_PATH")
    if not target or os.path.exists(target):
        return
    source = Path(__file__).resolve().parent.parent / "config.json"
    if not source.exists():
        return
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"  [startup] seeded {target} from repo config.json")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _seed_volume_config()
    _migrate_schema()
    reaped = _reap_orphan_runs()
    if reaped:
        print(f"  [startup] reaped {reaped} orphan run(s)")
    skills_module.sync_files_to_db()
    usage.install_hooks()

    # Search providers + fetcher config: both read from config.json at startup
    # and any time the providers settings change in the UI.
    try:
        import json as _json
        from . import fetcher as _fetcher
        with open(os.environ.get("CONFIG_PATH", "config.json"), encoding="utf-8") as _f:
            _cfg = _json.load(_f)
        search_providers.load_from_config(_cfg)
        _fetcher.configure(_cfg)
    except Exception as _e:
        print(f"  [startup] search_providers config load failed: {_e}")
    search_providers.install_scanner_hook()

    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(
    title="Competitor Watch",
    description="Visibility + control dashboard for the competitor watch service.",
    version="0.1.0",
    lifespan=lifespan,
)

@app.exception_handler(AuthenticationRequired)
async def _auth_required(request: Request, exc: AuthenticationRequired):
    """Turn 'no valid session' into the right shape for each caller:
    - HTMX: 200 + HX-Redirect header so htmx swaps a full navigation
    - JSON clients / API routes: 401 JSON
    - Browser page loads: 303 redirect to /login?next=<path>
    """
    path = request.url.path or "/"
    if request.headers.get("hx-request", "").lower() == "true":
        resp = Response(status_code=200)
        resp.headers["HX-Redirect"] = "/login"
        return resp
    accept = request.headers.get("accept", "")
    if path.startswith("/api/") or "application/json" in accept:
        return JSONResponse({"detail": exc.detail}, status_code=401)
    qs = f"?{request.url.query}" if request.url.query else ""
    nxt = f"{path}{qs}" if path != "/login" else "/"
    return RedirectResponse(f"/login?next={quote(nxt, safe='/')}", status_code=303)


app.include_router(auth_routes.router)
app.include_router(status.router)
app.include_router(competitors.router)
app.include_router(users_routes.router)
app.include_router(runs.router)
app.include_router(findings.router)
app.include_router(reports.router)
app.include_router(usage_routes.router)
app.include_router(skills_routes.router)
app.include_router(context_routes.router)
app.include_router(providers_routes.router)
app.include_router(env_keys_routes.router)
app.include_router(filters_routes.router)

# Jinja/HTMX UI — second renderer that consumes the same API shape.
app.include_router(ui.router)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
