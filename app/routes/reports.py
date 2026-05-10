from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..deps import get_db, get_current_user
from ..models import Report
from ..schemas import ReportOut, ReportDetail

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("", response_model=list[ReportOut])
def list_reports(
    limit: int = 50,
    kind: str | None = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    # `kind` filters to one report flavour (market_digest, market_releases).
    # The market-page regen-poll JS sets this so it doesn't see a fresh
    # report from the *other* tab and short-circuit the wait.
    q = db.query(Report)
    if kind:
        q = q.filter(Report.kind == kind)
    return q.order_by(Report.created_at.desc()).limit(limit).all()


@router.get("/{report_id}", response_model=ReportDetail)
def get_report(report_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    r = db.get(Report, report_id)
    if not r:
        raise HTTPException(404)
    return r
