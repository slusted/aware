from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from ..deps import get_db, get_current_user, require_role
from ..models import Run, RunEvent
from ..schemas import RunOut, RunEventOut
from .. import jobs

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", response_model=list[RunOut])
def list_runs(limit: int = 50, db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(Run).order_by(Run.started_at.desc()).limit(limit).all()


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404)
    return run


@router.get("/{run_id}/events", response_model=list[RunEventOut])
def run_events(run_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(RunEvent).filter(RunEvent.run_id == run_id).order_by(RunEvent.ts).all()


@router.post("/scan", status_code=202)
def trigger_scan(
    bg: BackgroundTasks,
    days: int | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Queue a manual scan. `days` is the freshness window — only return web
    content from the last N days. Omit for auto (= time since last successful scan)."""
    existing = db.query(Run).filter(Run.status.in_(["running", "cancelling"])).first()
    if existing:
        raise HTTPException(
            409,
            detail=f"scan already running (run #{existing.id}) — wait for it to finish",
        )
    bg.add_task(jobs.run_scan_job, "manual", days)
    return {"queued": True, "kind": "scan", "days": days}


@router.post("/discovery", status_code=202)
def trigger_discovery(bg: BackgroundTasks, _=Depends(require_role("admin"))):
    bg.add_task(jobs.run_discovery_job)
    return {"queued": True, "kind": "discovery"}


@router.post("/market-digest", status_code=202)
def trigger_market_digest(
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Regenerate the market digest over existing findings — LLM only, no
    new scraping. Rejected if another run is in flight so we don't double-
    write reports or compete for usage budget."""
    existing = db.query(Run).filter(Run.status.in_(["running", "cancelling"])).first()
    if existing:
        raise HTTPException(
            409,
            detail=f"run #{existing.id} ({existing.kind}) is already in flight — wait for it to finish",
        )
    bg.add_task(jobs.run_market_digest_job, "manual")
    return {"queued": True, "kind": "market_digest"}


@router.post("/{run_id}/cancel", status_code=202)
def cancel_run(
    run_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Request cooperative cancellation of an in-flight run. The worker checks
    a cancel flag at natural boundaries (between competitors, before review
    synthesis) and exits with status='cancelled'. The currently in-flight HTTP
    call completes — cancellation is best-effort, not instantaneous."""
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404)
    if run.status not in ("running", "cancelling"):
        raise HTTPException(409, f"run #{run_id} is {run.status}, not running")
    jobs.request_cancel(run_id)
    if run.status == "running":
        run.status = "cancelling"
        db.add(RunEvent(run_id=run_id, level="warn", message="cancellation requested"))
        db.commit()
    return {"cancelling": True, "run_id": run_id}
