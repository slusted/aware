import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..deps import get_db, get_current_user, require_role
from ..models import Run, RunEvent, MarketSynthesisReport
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


def _enforce_queue_cap(db: Session) -> None:
    """Soft cap on queued+in-flight runs (spec docs/runs/01-run-queue.md).
    Trigger endpoints call this before enqueueing so a runaway client
    doesn't fill the table with thousands of queued rows."""
    in_flight_or_queued = (
        db.query(Run)
        .filter(Run.status.in_(["queued", "running", "cancelling"]))
        .count()
    )
    if in_flight_or_queued >= jobs.RUN_QUEUE_MAX:
        raise HTTPException(
            429,
            detail=(
                f"run queue is full ({in_flight_or_queued}/{jobs.RUN_QUEUE_MAX}) — "
                "wait for some to finish or cancel a queued one"
            ),
        )


@router.post("/scan", status_code=202)
def trigger_scan(
    days: int | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Enqueue a manual scan. Returns 202 with the new run id and its
    FIFO queue position. Multiple triggers stack — the queue drainer
    runs them one at a time. `days` is the freshness window; omit for
    auto (= time since last successful scan)."""
    _enforce_queue_cap(db)
    run = jobs.enqueue_run(
        db,
        "scan",
        triggered_by="manual",
        job_args={"days": days},
    )
    return {
        "queued": True,
        "kind": "scan",
        "days": days,
        "run_id": run.id,
        "queue_position": jobs.queue_position(db, run.id),
    }


@router.post("/discovery", status_code=202)
def trigger_discovery(
    db: Session = Depends(get_db),
    _=Depends(require_role("admin")),
):
    _enforce_queue_cap(db)
    run = jobs.enqueue_run(db, "discovery", triggered_by="manual")
    return {
        "queued": True,
        "kind": "discovery",
        "run_id": run.id,
        "queue_position": jobs.queue_position(db, run.id),
    }


@router.post("/market-digest", status_code=202)
def trigger_market_digest(
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Enqueue a market-digest regen over existing findings — LLM only, no
    new scraping. Returns 202; the drainer picks it up when nothing
    else is in flight."""
    _enforce_queue_cap(db)
    run = jobs.enqueue_run(db, "market_digest", triggered_by="manual")
    return {
        "queued": True,
        "kind": "market_digest",
        "run_id": run.id,
        "queue_position": jobs.queue_position(db, run.id),
    }


# Manual cooldown default: 6h. Within this window, an extra Run click is
# refused unless `force=1` is passed (which the UI supplies after the user
# confirms the "already fresh" prompt). The cron path ignores this — it's
# expected to fire on schedule regardless of recency.
_MARKET_SYNTHESIS_COOLDOWN_S = int(
    os.environ.get("MARKET_SYNTHESIS_COOLDOWN_S", str(6 * 3600))
)


@router.post("/market-synthesis", status_code=202)
def trigger_market_synthesis(
    request: Request,
    bg: BackgroundTasks,
    agent: str = Form("preview"),
    brief: str | None = Form(None),
    window_days: int = Form(30),
    force: int = Form(0),
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Kick off a cross-competitor market synthesis (spec 05). Runs Gemini
    Deep Research over last-{window_days}-days findings + per-competitor
    reviews + per-competitor DR excerpts and writes a MarketSynthesisReport.

    Accepts the operator-edited `brief` from /partials/synthesis_run_form;
    when it's missing or blank, the job composes the default brief
    itself (legacy one-click path / curl).

    Global singleton: 409 if another synthesis is already in flight (cron or
    manual). `force=1` bypasses the 6h freshness cooldown for reruns.

    Response shape depends on the caller: browser form posts get a 303 back
    to /market so the page reloads into the "queued" status card; JSON
    clients get a 202 with the queued metadata."""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise HTTPException(
            400,
            detail="GEMINI_API_KEY is not set. Add it on /settings/keys before running synthesis.",
        )

    in_flight = (
        db.query(MarketSynthesisReport)
        .filter(MarketSynthesisReport.status.in_(["queued", "running"]))
        .first()
    )
    if in_flight:
        raise HTTPException(
            409,
            detail=f"market synthesis #{in_flight.id} is already in flight — wait for it to finish",
        )

    if not force:
        latest_ready = (
            db.query(MarketSynthesisReport)
            .filter(MarketSynthesisReport.status == "ready")
            .order_by(MarketSynthesisReport.started_at.desc())
            .first()
        )
        if latest_ready and latest_ready.started_at:
            age = (datetime.utcnow() - latest_ready.started_at).total_seconds()
            if age < _MARKET_SYNTHESIS_COOLDOWN_S:
                raise HTTPException(
                    429,
                    detail=(
                        f"Synthesis #{latest_ready.id} is only "
                        f"{int(age // 60)} min old — pass force=1 to run anyway."
                    ),
                )

    agent_resolved = agent if agent in ("preview", "max") else "preview"
    window = max(1, min(int(window_days), 365))
    submitted_brief = (brief or "").strip() or None

    bg.add_task(
        jobs.run_market_synthesis_job,
        "manual",
        agent_resolved,
        window,
        submitted_brief,
    )

    # Browser form submits want a redirect; fetch/XHR callers want JSON.
    # Distinguish by content-type — form posts arrive as
    # application/x-www-form-urlencoded or multipart/form-data.
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/x-www-form-urlencoded") or ctype.startswith("multipart/form-data"):
        return RedirectResponse("/market", status_code=303)

    return {
        "queued": True,
        "kind": "market_synthesis",
        "agent": agent_resolved,
        "window_days": window,
        "brief_was_edited": submitted_brief is not None,
    }


@router.post("/{run_id}/cancel", status_code=202)
def cancel_run(
    run_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Cancel a queued or running run.

    - Queued: flips to 'cancelled' immediately. The drainer skips it.
    - Running: cooperative — sets 'cancelling', the worker bails at the
      next checkpoint (between competitors, before review synthesis).
      The currently in-flight HTTP call completes; cancellation is
      best-effort, not instantaneous.
    """
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404)
    if run.status == "queued":
        run.status = "cancelled"
        run.finished_at = datetime.utcnow()
        db.add(
            RunEvent(
                run_id=run_id,
                level="warn",
                message="cancelled before start",
            )
        )
        db.commit()
        return {"cancelled": True, "run_id": run_id}
    if run.status not in ("running", "cancelling"):
        raise HTTPException(409, f"run #{run_id} is {run.status}, not running")
    jobs.request_cancel(run_id)
    if run.status == "running":
        run.status = "cancelling"
        db.add(RunEvent(run_id=run_id, level="warn", message="cancellation requested"))
        db.commit()
    return {"cancelling": True, "run_id": run_id}
