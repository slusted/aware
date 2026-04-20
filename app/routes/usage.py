from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from ..deps import get_db, get_current_user
from ..models import UsageEvent

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _sum_since(db: Session, since: datetime) -> dict:
    q = db.query(
        func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
        func.coalesce(func.sum(UsageEvent.input_tokens), 0),
        func.coalesce(func.sum(UsageEvent.output_tokens), 0),
        func.coalesce(func.sum(UsageEvent.credits), 0),
        func.count(UsageEvent.id),
    ).filter(UsageEvent.ts >= since)
    cost, input_tok, output_tok, credits, calls = q.one()
    return {
        "cost_usd": round(float(cost), 4),
        "input_tokens": int(input_tok),
        "output_tokens": int(output_tok),
        "tavily_credits": int(credits),
        "call_count": int(calls),
    }


@router.get("/summary")
def summary(db: Session = Depends(get_db), _=Depends(get_current_user)):
    now = datetime.utcnow()
    return {
        "today": _sum_since(db, now - timedelta(days=1)),
        "week":  _sum_since(db, now - timedelta(days=7)),
        "month": _sum_since(db, now - timedelta(days=30)),
        "all":   _sum_since(db, datetime(2000, 1, 1)),
    }


@router.get("/by_model")
def by_model(days: int = 30, db: Session = Depends(get_db), _=Depends(get_current_user)):
    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(
            UsageEvent.provider,
            UsageEvent.model,
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(UsageEvent.credits), 0),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
        )
        .filter(UsageEvent.ts >= since)
        .group_by(UsageEvent.provider, UsageEvent.model)
        .order_by(func.sum(UsageEvent.cost_usd).desc())
        .all()
    )
    return [
        {
            "provider": p, "model": m, "calls": c,
            "input_tokens": int(it), "output_tokens": int(ot),
            "credits": int(cr), "cost_usd": round(float(cost), 4),
        }
        for p, m, c, it, ot, cr, cost in rows
    ]


@router.get("/by_run")
def by_run(limit: int = 30, db: Session = Depends(get_db), _=Depends(get_current_user)):
    rows = (
        db.query(
            UsageEvent.run_id,
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
        )
        .filter(UsageEvent.run_id.isnot(None))
        .group_by(UsageEvent.run_id)
        .order_by(UsageEvent.run_id.desc())
        .limit(limit)
        .all()
    )
    return [
        {"run_id": rid, "calls": c, "cost_usd": round(float(cost), 4)}
        for rid, c, cost in rows
    ]
