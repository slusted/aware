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

from .db import Base, SessionLocal, engine
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


def _reset_db_if_requested():
    """Escape hatch for a wedged DB. Set RESET_DB_ONCE=1 in the Railway
    dashboard, redeploy, then unset it. Deletes app.db plus its WAL/SHM
    sidecars so alembic can rebuild the schema from scratch. Guarded by
    an env var so it never fires accidentally."""
    if os.environ.get("RESET_DB_ONCE") != "1":
        return
    data_dir = os.environ.get("DATA_DIR", "data")
    db_path = Path(data_dir) / "app.db"
    removed = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
            removed.append(p.name)
    print(f"  [startup] RESET_DB_ONCE=1 → removed {removed or 'nothing (already clean)'}", flush=True)
    print("  [startup] REMEMBER to unset RESET_DB_ONCE in Railway after this deploy succeeds", flush=True)


def _migrate_schema():
    """Bootstrap schema at startup, with self-healing for stale-version DBs.

    History: running the alembic chain against the Railway SQLite volume
    was crash-looping silently inside every batch_alter_table migration,
    and digging layer by layer wasn't converging.

    The fix: if the DB is at the latest head, noop. If it's behind (stale
    schema, the case that kept wedging us at 5a4568830d8c) *and* nothing
    of value lives there yet, wipe and rebuild from models.py via
    Base.metadata.create_all() in one step — no chain, no batch mode, no
    room to fail. Guarded by an emptiness check so this can't clobber a
    real DB later: it only auto-wipes when users, findings, competitors,
    and runs are all empty.
    """
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect, text

    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    head_rev = script.get_current_head()

    insp = inspect(engine)
    tables = set(insp.get_table_names())

    current_rev: str | None = None
    if "alembic_version" in tables:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            current_rev = row[0] if row else None

    if current_rev == head_rev:
        print(f"  [startup] DB already at head ({head_rev})", flush=True)
        return

    if current_rev is None:
        # Fresh DB (or legacy create_all DB with no alembic_version). Build
        # the schema from models.py and stamp head.
        print(f"  [startup] fresh DB → create_all + stamp {head_rev}", flush=True)
        Base.metadata.create_all(bind=engine)
        command.stamp(cfg, head_rev)
        return

    # Behind head. Check whether anything of value is in the DB — if yes,
    # bail loudly so a human can decide. If no, self-heal: wipe and rebuild.
    row_count = 0
    for t in ("users", "findings", "competitors", "runs"):
        if t in tables:
            with engine.connect() as conn:
                r = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).fetchone()
                row_count += int(r[0] or 0)
    if row_count > 0:
        raise RuntimeError(
            f"DB at revision {current_rev}, codebase head is {head_rev}, "
            f"and {row_count} rows exist across users/findings/competitors/runs. "
            "Refusing to auto-wipe. Run the alembic chain manually or set "
            "RESET_DB_ONCE=1 if the data is disposable."
        )

    data_dir = os.environ.get("DATA_DIR", "data")
    db_path = Path(data_dir) / "app.db"
    print(
        f"  [startup] DB at stale revision {current_rev} (head is {head_rev}), "
        "all user-facing tables empty → wiping and rebuilding",
        flush=True,
    )
    engine.dispose()
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    Base.metadata.create_all(bind=engine)
    command.stamp(cfg, head_rev)
    print(f"  [startup] wiped + create_all + stamp {head_rev} complete", flush=True)


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
    _reset_db_if_requested()
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
