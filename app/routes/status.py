from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from ..deps import get_db, get_current_user
from ..models import Run, Finding, Competitor
from ..schemas import StatusOut, RunOut
from .. import scheduler

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("", response_model=StatusOut)
def status(db: Session = Depends(get_db), _=Depends(get_current_user)):
    last_run = db.query(Run).order_by(Run.started_at.desc()).first()
    is_running = (
        db.query(Run).filter(Run.status == "running").first() is not None
    )
    since = datetime.utcnow() - timedelta(days=1)
    findings_today = db.query(func.count(Finding.id)).filter(Finding.created_at >= since).scalar() or 0
    competitor_count = db.query(func.count(Competitor.id)).filter(Competitor.active == True).scalar() or 0
    return StatusOut(
        last_run=RunOut.model_validate(last_run) if last_run else None,
        next_run_at=scheduler.next_run_at("daily_scan"),
        is_running=is_running,
        competitor_count=competitor_count,
        findings_today=findings_today,
    )
