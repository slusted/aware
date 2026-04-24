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
    # ENV_PATH points at a .env on a persistent volume (Railway/Render/Fly),
    # so UI-managed keys survive redeploys. override=True is load-bearing:
    # without it, a stale value in the Railway dashboard Variables tab wins
    # over a newer value the user set via the Settings UI, and the UI change
    # silently reverts on the next redeploy. The volume is the source of
    # truth for keys the UI manages; dashboard vars are bootstrap-only.
    load_dotenv(os.environ.get("ENV_PATH") or ".env", override=True)
except ImportError:
    pass

from datetime import datetime

from .db import Base, SessionLocal, engine
from . import scheduler, skills as skills_module, ui, usage, search_providers
from .models import Run, RunEvent
from .routes import status, competitors, runs, findings, reports, usage as usage_routes, skills as skills_routes, context as context_routes, providers as providers_routes, env_keys as env_keys_routes, filters as filters_routes, auth as auth_routes, users as users_routes, signal_events as signal_events_routes, preferences as preferences_routes


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

    # Behind head. Count real data so we know whether wipe-and-rebuild is
    # safe as a last-resort fallback.
    row_count = 0
    for t in ("users", "findings", "competitors", "runs"):
        if t in tables:
            with engine.connect() as conn:
                r = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).fetchone()
                row_count += int(r[0] or 0)

    if row_count > 0:
        # Data exists. Wipe is off the table. Try the alembic chain — many
        # migrations (CREATE TABLE, add-column without batch_alter_table)
        # apply cleanly against a live DB. Only bail when the chain itself
        # fails, which is the batch_alter_table crash-loop scenario that
        # originally motivated this guard.
        print(
            f"  [startup] DB at {current_rev}, head is {head_rev}, "
            f"{row_count} rows present — running alembic upgrade",
            flush=True,
        )
        try:
            command.upgrade(cfg, head_rev)
        except Exception as e:
            raise RuntimeError(
                f"Alembic upgrade from {current_rev} to {head_rev} failed: {e}. "
                "The DB has real data so auto-wipe is refused. Inspect the "
                "migration manually via `railway run alembic upgrade head`, or "
                "set RESET_DB_ONCE=1 if the data is disposable."
            ) from e
        print(f"  [startup] upgrade complete, now at {head_rev}", flush=True)
        return

    # Stale + empty — the chain has historically crash-looped in this
    # combination, so skip it entirely and rebuild from models.py.
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
    from .seed import seed_competitors
    _db = SessionLocal()
    try:
        added, reactivated = seed_competitors(_db)
        if added or reactivated:
            print(f"  [startup] seed_competitors: +{added} new, {reactivated} reactivated", flush=True)
    except Exception as _e:
        print(f"  [startup] seed_competitors failed: {_e}", flush=True)
    finally:
        _db.close()

    # Warm the logo cache in a background thread — any competitor with a
    # homepage_domain but no on-disk file gets one fetch attempt. Boot
    # proceeds immediately; failures are logged, not raised.
    import threading
    from . import logos as _logos

    def _warm_logos():
        _warm_db = SessionLocal()
        try:
            attempted, ok = _logos.refetch_missing(_warm_db)
            if attempted:
                print(f"  [startup] logos: fetched {ok}/{attempted} missing", flush=True)
        except Exception as _e:
            print(f"  [startup] logo warm failed: {_e}", flush=True)
        finally:
            _warm_db.close()

    threading.Thread(target=_warm_logos, name="logo-warmup", daemon=True).start()
    reaped = _reap_orphan_runs()
    if reaped:
        print(f"  [startup] reaped {reaped} orphan run(s)")

    # Resume any Gemini Deep Research runs that were in flight when the
    # previous process died. Rows with a live Gemini interaction_id get a
    # background polling thread; rows that never got an id are marked
    # failed. Runs on every boot — cheap when no rows match.
    try:
        from .jobs import resume_in_flight_research
        resumed = resume_in_flight_research()
        if resumed:
            print(f"  [startup] resumed {resumed} deep-research report(s)")
    except Exception as _e:
        print(f"  [startup] deep-research resume failed: {_e}", flush=True)

    # Same resume-on-boot semantics for cross-competitor market syntheses
    # (spec 05). Runs independently of the deep-research sweep so a failure
    # in one doesn't stall the other.
    try:
        from .jobs import resume_in_flight_market_synthesis
        resumed = resume_in_flight_market_synthesis()
        if resumed:
            print(f"  [startup] resumed {resumed} market-synthesis report(s)")
    except Exception as _e:
        print(f"  [startup] market-synthesis resume failed: {_e}", flush=True)
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
app.include_router(signal_events_routes.router)
app.include_router(preferences_routes.router)
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

# Cached competitor logos live under DATA_DIR/logos so they survive deploys
# on Railway's mounted volume. Ensure the dir exists before StaticFiles
# resolves it (StaticFiles errors on a missing path).
from . import logos as _logos_module
_logos_module.LOGOS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/logos", StaticFiles(directory=str(_logos_module.LOGOS_DIR)), name="logos")
