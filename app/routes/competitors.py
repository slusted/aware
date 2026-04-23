from datetime import datetime
import json, os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..deps import get_db, get_current_user, require_role
from ..models import Competitor, CompetitorReport, PositioningSnapshot, Run, DeepResearchReport
from ..schemas import CompetitorOut, CompetitorIn
from ..config_sync import sync_db_to_config
from .. import logos as logos_cache

router = APIRouter(prefix="/api/competitors", tags=["competitors"])


def _load_company_context():
    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return (
        cfg.get("company", "Seek"),
        cfg.get("industry", "job search and recruitment platforms"),
    )


@router.get("/autofill/stream")
def autofill_competitor_stream(
    name: str,
    competitor_id: int | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Server-Sent Events stream for the autofill agent. Emits `progress`
    events for each tool call and a final `done` event with the CompetitorIn
    dict.

    If `competitor_id` is provided, the agent is told to refine/extend the
    existing row's values rather than fill from blank — useful for
    optimising a competitor already on the watchlist."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    existing: dict | None = None
    performance_report: str | None = None
    if competitor_id is not None:
        c = db.get(Competitor, competitor_id)
        if not c:
            raise HTTPException(404, f"competitor id {competitor_id} not found")
        existing = {
            "category": c.category,
            "threat_angle": c.threat_angle,
            "keywords": c.keywords or [],
            "subreddits": c.subreddits or [],
            "careers_domains": c.careers_domains or [],
            "newsroom_domains": c.newsroom_domains or [],
            "homepage_domain": c.homepage_domain,
            "app_store_id": c.app_store_id,
            "play_package": c.play_package,
            "trends_keyword": c.trends_keyword,
            "min_relevance_score": c.min_relevance_score,
            "social_score_multiplier": c.social_score_multiplier,
        }
        # Feed the agent the finding history so it can prune dead weight
        # instead of just refining the same fields blindly. Empty string
        # when the competitor has no findings yet (new or quiet).
        from ..competitor_performance import build_performance_report
        performance_report = build_performance_report(db, competitor_id, days=60) or None

    company, industry = _load_company_context()
    from ..competitor_autofill import autofill_stream

    def _gen():
        try:
            for event in autofill_stream(
                name, company=company, industry=industry,
                existing=existing, performance_report=performance_report,
            ):
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        except Exception as e:
            err = {"type": "error", "message": f"{type(e).__name__}: {e}"}
            yield f"event: error\ndata: {json.dumps(err)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("", response_model=list[CompetitorOut])
def list_competitors(
    active_only: bool = True,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    q = db.query(Competitor)
    if active_only:
        q = q.filter(Competitor.active == True)
    return q.order_by(Competitor.name).all()


@router.get("/{competitor_id}", response_model=CompetitorOut)
def get_competitor(competitor_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    return c


@router.post("", response_model=CompetitorOut, status_code=201)
def create_competitor(
    payload: CompetitorIn,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    if db.query(Competitor).filter(Competitor.name == payload.name).first():
        raise HTTPException(409, f"competitor '{payload.name}' already exists")
    c = Competitor(
        name=payload.name,
        category=payload.category,
        threat_angle=payload.threat_angle,
        keywords=payload.keywords,
        subreddits=payload.subreddits,
        careers_domains=payload.careers_domains,
        newsroom_domains=payload.newsroom_domains,
        ats_tenants=payload.ats_tenants,
        homepage_domain=payload.homepage_domain,
        app_store_id=payload.app_store_id,
        play_package=payload.play_package,
        trends_keyword=payload.trends_keyword,
        min_relevance_score=payload.min_relevance_score,
        social_score_multiplier=payload.social_score_multiplier,
        positioning_pages=payload.positioning_pages or [],
        source="manual",
        active=True,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    sync_db_to_config(db)
    if payload.homepage_domain:
        bg.add_task(logos_cache.fetch_and_store, payload.homepage_domain)
    return c


@router.put("/{competitor_id}", response_model=CompetitorOut)
def update_competitor(
    competitor_id: int,
    payload: CompetitorIn,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    # Rename collision check
    if payload.name != c.name and db.query(Competitor).filter(Competitor.name == payload.name).first():
        raise HTTPException(409, f"competitor '{payload.name}' already exists")
    prior_domain = c.homepage_domain
    c.name = payload.name
    c.category = payload.category
    c.threat_angle = payload.threat_angle
    c.keywords = payload.keywords
    c.subreddits = payload.subreddits
    c.careers_domains = payload.careers_domains
    c.newsroom_domains = payload.newsroom_domains
    c.ats_tenants = payload.ats_tenants
    c.homepage_domain = payload.homepage_domain
    c.app_store_id = payload.app_store_id
    c.play_package = payload.play_package
    c.trends_keyword = payload.trends_keyword
    c.min_relevance_score = payload.min_relevance_score
    c.social_score_multiplier = payload.social_score_multiplier
    c.positioning_pages = payload.positioning_pages or []
    db.commit()
    db.refresh(c)
    sync_db_to_config(db)
    # Re-fetch when the domain changed or we've never cached one yet.
    if payload.homepage_domain and (
        payload.homepage_domain != prior_domain
        or not logos_cache.has_logo(payload.homepage_domain)
    ):
        bg.add_task(logos_cache.fetch_and_store, payload.homepage_domain)
    return c


@router.delete("/{competitor_id}", status_code=204)
def delete_competitor(
    competitor_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin")),
):
    """Soft delete: flag inactive and drop from config.json. History (findings,
    past reports) stays intact — the engine simply stops scanning them."""
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    c.active = False
    db.commit()
    sync_db_to_config(db)


@router.post("/{competitor_id}/restore", response_model=CompetitorOut)
def restore_competitor(
    competitor_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin")),
):
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    c.active = True
    db.commit()
    db.refresh(c)
    sync_db_to_config(db)
    return c


def _regen_one(competitor_id: int):
    """Background task: regenerate one competitor's strategy review."""
    from ..db import SessionLocal
    from ..competitor_reports import synthesize
    from ..usage import current_run_id
    import json, os
    db = SessionLocal()
    try:
        c = db.get(Competitor, competitor_id)
        if not c:
            return
        cfg_path = os.environ.get("CONFIG_PATH", "config.json")
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        synthesize(db, c,
                   company=cfg.get("company", "Seek"),
                   industry=cfg.get("industry", "job search and recruitment platforms"))
    finally:
        db.close()


@router.post("/{competitor_id}/reports", status_code=202)
def regenerate_competitor_report(
    competitor_id: int,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Fire off a fresh strategy review for this competitor — LLM-only, no
    new scraping. Uses existing findings + prior review + recent market
    digests as context. Returns immediately; the UI polls
    /api/competitors/{id}/reports to pick up the new row."""
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    bg.add_task(_regen_one, competitor_id)
    return {"queued": True, "competitor_id": competitor_id, "kind": "regen_review"}


@router.post("/{competitor_id}/scan", status_code=202)
def trigger_competitor_scan(
    competitor_id: int,
    bg: BackgroundTasks,
    days: int | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """End-to-end scan for one competitor: web search (Tavily + Serper if
    enabled) → save findings → synthesize a fresh strategy review. Shows up
    as a Run (kind='competitor_scan') with live event streaming.

    Rejected if any other run is in flight — scanner's `memory` dict mutation
    plus config.json sync are the serialization points."""
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    existing = db.query(Run).filter(Run.status.in_(["running", "cancelling"])).first()
    if existing:
        raise HTTPException(
            409,
            detail=f"run #{existing.id} ({existing.kind}) is already in flight — wait for it to finish",
        )
    from ..jobs import run_competitor_scan_job
    bg.add_task(run_competitor_scan_job, competitor_id, "manual", days)
    return {"queued": True, "competitor_id": competitor_id, "kind": "competitor_scan", "days": days}


@router.post("/{competitor_id}/positioning/refresh")
def refresh_positioning(
    competitor_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Fetch marketing pages + rerun the positioning pipeline synchronously.
    Expect ~30s. Redirects to the Positioning tab on success or failure;
    the tab renders a muted error line if the snapshot couldn't be written."""
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    from ..signals.positioning import extract_positioning
    try:
        extract_positioning(c, db)
    except Exception as e:
        # Swallow: the tab will still render whatever the last snapshot was
        # (or the empty state). We surface the error via ?err=… so the
        # template can show it without a flash-message subsystem.
        import urllib.parse
        msg = urllib.parse.quote(f"{type(e).__name__}: {e}"[:200])
        return RedirectResponse(
            f"/competitors/{competitor_id}?positioning_err={msg}#positioning",
            status_code=303,
        )
    return RedirectResponse(
        f"/competitors/{competitor_id}#positioning", status_code=303
    )


# Minimum age (seconds) for the latest ready report before the Run button
# skips the cooldown confirm. Matches the 24h guardrail in the spec — the
# UI reads this to render a soft "the report is fresh" hint, but the server
# never hard-blocks; deep research is user-triggered and the user is adult
# enough to decide.
DEEP_RESEARCH_COOLDOWN_S = int(os.environ.get("DEEP_RESEARCH_COOLDOWN_S", str(24 * 60 * 60)))


@router.post("/{competitor_id}/research/run")
def run_deep_research(
    competitor_id: int,
    bg: BackgroundTasks,
    agent: str = "preview",
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """Kick off a Gemini Deep Research run for one competitor. Creates the
    DeepResearchReport row in 'queued', enqueues the background job, and
    redirects to the Research tab — the tab picks up the running state on
    next render and HTMX-polls until terminal.

    Enforces a global concurrency cap so we don't stampede Gemini or burn
    budget by accident.
    """
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)

    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise HTTPException(
            400,
            detail="GEMINI_API_KEY is not set. Add it on /settings/keys before running research.",
        )

    from ..jobs import (
        DEEP_RESEARCH_MAX_CONCURRENT,
        current_research_load,
        run_deep_research_job,
        _build_research_brief,
    )
    load = current_research_load(db)
    if load >= DEEP_RESEARCH_MAX_CONCURRENT:
        raise HTTPException(
            409,
            detail=(
                f"{load} deep-research runs already in flight "
                f"(cap={DEEP_RESEARCH_MAX_CONCURRENT}). Wait for one to finish."
            ),
        )

    # Pre-build the brief so the row on disk reflects exactly what will be
    # sent to Gemini. The job still rebuilds if the brief column is empty,
    # but persisting up front keeps the audit trail honest.
    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    brief = _build_research_brief(c, cfg)

    # Normalize agent choice at the edge so the DB stores the canonical form.
    resolved_agent = "max" if (agent or "").lower().strip() == "max" else "preview"

    report = DeepResearchReport(
        competitor_id=competitor_id,
        agent=resolved_agent,
        status="queued",
        brief=brief,
        started_at=datetime.utcnow(),
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    bg.add_task(run_deep_research_job, competitor_id, report.id, resolved_agent, "manual")
    return RedirectResponse(
        f"/competitors/{competitor_id}#research",
        status_code=303,
    )


@router.get("/{competitor_id}/research")
def list_deep_research_reports(
    competitor_id: int,
    limit: int = 20,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    rows = (
        db.query(DeepResearchReport)
        .filter(DeepResearchReport.competitor_id == competitor_id)
        .order_by(DeepResearchReport.started_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "status": r.status,
            "agent": r.agent,
            "model": r.model,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "cost_usd": r.cost_usd,
            "sources_count": len(r.sources or []),
            "has_body": bool(r.body_md),
            "error": r.error,
        }
        for r in rows
    ]


@router.get("/{competitor_id}/reports")
def list_competitor_reports(
    competitor_id: int,
    limit: int = 20,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    rows = (
        db.query(CompetitorReport)
        .filter(CompetitorReport.competitor_id == competitor_id)
        .order_by(CompetitorReport.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "run_id": r.run_id,
            "model": r.model,
            "source_summary": r.source_summary,
            "created_at": r.created_at,
            "body_md": r.body_md,
        }
        for r in rows
    ]
