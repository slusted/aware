import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

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
from .routes import status, competitors, runs, findings, reports, usage as usage_routes, skills as skills_routes, context as context_routes, providers as providers_routes, env_keys as env_keys_routes, filters as filters_routes


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
    # Phase 1: create tables if missing (Alembic owns the schema in prod).
    Base.metadata.create_all(bind=engine)
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

app.include_router(status.router)
app.include_router(competitors.router)
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
