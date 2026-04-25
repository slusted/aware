"""HTMX/Jinja renderer. Calls the same DB the JSON API uses.
When React lands later, this module is deleted; the JSON API is unchanged.
"""
import os
from pathlib import Path
from datetime import datetime, timedelta
from fastapi import APIRouter, BackgroundTasks, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

from sqlalchemy import func as _func

from .deps import get_db, get_current_user
from .models import Run, Finding, Competitor, CompetitorReport, PositioningSnapshot, Report, UsageEvent, CompetitorMetric, SignalView, SavedFilter, DeepResearchReport, UserSignalEvent, MarketSynthesisReport, CompetitorCandidate
from . import scheduler
from .ranker.present import present as _present_clusters, lead_findings as _lead_findings
from .routes.signal_events import emit_shown_events


# Signal-stream taxonomy, surfaced in the stream filter bar. Order here
# drives the order of filter chips in the UI.
SIGNAL_TYPES = [
    ("funding",        "Funding"),
    ("new_hire",       "Hires"),
    ("product_launch", "Launches"),
    ("integration",    "Integrations"),
    ("price_change",   "Pricing"),
    ("messaging_shift","Messaging"),
    ("voc_mention",    "VoC"),
    ("news",           "News"),
    ("momentum_point", "Momentum"),
    ("other",          "Other"),
]

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(include_in_schema=False)


@router.get("/")
def root_to_stream():
    # Stream is the default landing surface; the dashboard is an admin view
    # now reachable at /dashboard (and still linked from the sidebar).
    return RedirectResponse("/stream", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    last_run = db.query(Run).order_by(Run.started_at.desc()).first()
    recent_runs = db.query(Run).order_by(Run.started_at.desc()).limit(10).all()
    recent_findings = db.query(Finding).order_by(Finding.created_at.desc()).limit(20).all()
    since = datetime.utcnow() - timedelta(days=1)
    findings_today = db.query(func.count(Finding.id)).filter(Finding.created_at >= since).scalar() or 0
    competitor_count = db.query(func.count(Competitor.id)).filter(Competitor.active == True).scalar() or 0
    is_running = db.query(Run).filter(Run.status == "running").first() is not None
    cost_today = db.query(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0)).filter(UsageEvent.ts >= since).scalar() or 0.0

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "last_run": last_run,
        "next_run_at": scheduler.next_run_at("daily_scan"),
        "is_running": is_running,
        "findings_today": findings_today,
        "competitor_count": competitor_count,
        "recent_runs": recent_runs,
        "recent_findings": recent_findings,
        "cost_today": float(cost_today),
    })


@router.get("/company", response_class=HTMLResponse)
def company_page(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _context_page(request, db, user, scope="company", title="Company")


@router.get("/customer", response_class=HTMLResponse)
def customer_page(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _context_page(request, db, user, scope="customer", title="Customer")


def _context_page(request, db, user, *, scope: str, title: str):
    from .models import ContextBrief, Document, Finding
    latest = (
        db.query(ContextBrief)
        .filter(ContextBrief.scope == scope)
        .order_by(ContextBrief.created_at.desc())
        .first()
    )
    history = (
        db.query(ContextBrief)
        .filter(ContextBrief.scope == scope)
        .order_by(ContextBrief.created_at.desc())
        .offset(1)
        .limit(10)
        .all()
    )
    docs = (
        db.query(Document)
        .filter(Document.bucket == scope)
        .order_by(Document.created_at.desc())
        .all()
    )

    # Customer scope gets an extra panel: recent aggregated discussion from
    # the customer_watch sweep.
    discussion = []
    watch = None
    if scope == "customer":
        # Reddit VoC findings — pulled regardless of whether they came from the
        # customer Full scan or from an individual competitor's full scan. Both
        # save with topic="voice of customer" and source="reddit/r/<sub>".
        discussion = (
            db.query(Finding)
            .filter(Finding.topic == "voice of customer")
            .filter(Finding.source.like("reddit/%"))
            .order_by(Finding.created_at.desc())
            .limit(50)
            .all()
        )
        import json, os
        try:
            with open(os.environ.get("CONFIG_PATH", "config.json"), encoding="utf-8") as f:
                watch = (json.load(f).get("customer_watch") or {})
        except Exception:
            watch = {}

    return templates.TemplateResponse(request, "context_page.html", {
        "user": user,
        "scope": scope,
        "title": title,
        "latest": latest,
        "history": history,
        "docs": docs,
        "discussion": discussion,
        "watch": watch,
    })


@router.get("/competitors", response_class=HTMLResponse)
def competitors_index(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    rows = db.query(Competitor).filter(Competitor.active == True).order_by(Competitor.name).all()
    # latest CompetitorReport per competitor + finding count in last 30d
    since = datetime.utcnow() - timedelta(days=30)
    summaries = []
    logos: dict[int, str] = {}
    for c in rows:
        latest = (
            db.query(CompetitorReport)
            .filter(CompetitorReport.competitor_id == c.id)
            .order_by(CompetitorReport.created_at.desc())
            .first()
        )
        recent_findings = (
            db.query(_func.count(Finding.id))
            .filter(Finding.competitor == c.name)
            .filter(Finding.created_at >= since)
            .scalar() or 0
        )
        summaries.append({"c": c, "latest": latest, "recent_findings": recent_findings})
        if c.homepage_domain:
            logos[c.id] = f"https://logos-api.apistemic.com/domain:{c.homepage_domain}?fallback=monogram"
    return templates.TemplateResponse(request, "competitors_index.html", {
        "user": user, "items": summaries, "logos": logos,
    })


@router.get("/competitors/{competitor_id}", response_class=HTMLResponse)
def competitor_profile(competitor_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    latest = (
        db.query(CompetitorReport)
        .filter(CompetitorReport.competitor_id == c.id)
        .order_by(CompetitorReport.created_at.desc())
        .first()
    )
    history = (
        db.query(CompetitorReport)
        .filter(CompetitorReport.competitor_id == c.id)
        .order_by(CompetitorReport.created_at.desc())
        .offset(1)
        .limit(10)
        .all()
    )
    findings = (
        db.query(Finding)
        .filter(Finding.competitor == c.name)
        .order_by(Finding.created_at.desc())
        .limit(30)
        .all()
    )
    # Per-provider aggregate so the user can see at a glance which provider
    # delivered the most results and how relevant they were for this competitor.
    since = datetime.utcnow() - timedelta(days=30)
    breakdown_rows = (
        db.query(
            Finding.search_provider.label("provider"),
            func.count(Finding.id).label("count"),
            func.avg(Finding.score).label("avg_score"),
        )
        .filter(Finding.competitor == c.name, Finding.created_at >= since)
        .group_by(Finding.search_provider)
        .order_by(func.count(Finding.id).desc())
        .all()
    )
    provider_breakdown = [
        {"provider": r.provider, "count": r.count, "avg_score": r.avg_score}
        for r in breakdown_rows
    ]

    # ── Momentum time-series for this competitor ──────────────────
    # For each metric we track, pull the last 30 days of values (oldest → newest)
    # for a sparkline, plus the latest value and its delta vs ~7 days ago.
    momentum_since = datetime.utcnow() - timedelta(days=30)
    raw_metrics = (
        db.query(CompetitorMetric)
        .filter(
            CompetitorMetric.competitor_id == c.id,
            CompetitorMetric.collected_at >= momentum_since,
        )
        .order_by(CompetitorMetric.metric, CompetitorMetric.collected_at)
        .all()
    )
    series: dict[str, list[CompetitorMetric]] = {}
    for m in raw_metrics:
        series.setdefault(m.metric, []).append(m)

    # How to render each known metric. direction="lower_better" means smaller
    # is better (rank). Anything else = higher better (installs, rating, trends).
    METRIC_DISPLAY = {
        "google_trends": {"label": "Google Trends", "unit": "/100", "direction": "higher_better"},
        "ios_rank":      {"label": "iOS App Store rank", "unit": "", "direction": "lower_better", "prefix": "#"},
        "play_installs": {"label": "Play Store installs (min)", "unit": "", "direction": "higher_better"},
        "play_rating":   {"label": "Play Store rating", "unit": "/5", "direction": "higher_better"},
        "play_reviews":  {"label": "Play Store reviews", "unit": "", "direction": "higher_better"},
    }
    momentum = []
    for metric_name, display in METRIC_DISPLAY.items():
        points = series.get(metric_name, [])
        if not points:
            continue
        latest_pt = points[-1]
        # Find the point closest to 7 days before latest for the delta
        target = latest_pt.collected_at - timedelta(days=7)
        earlier = min(points, key=lambda p: abs((p.collected_at - target).total_seconds()))
        delta = None
        if latest_pt.value is not None and earlier.value is not None and earlier.id != latest_pt.id:
            delta = latest_pt.value - earlier.value
        # Build a compact spark series (just value + date) for the template
        spark = [
            {"v": p.value, "d": p.collected_date}
            for p in points if p.value is not None
        ]
        momentum.append({
            "metric": metric_name,
            "label": display["label"],
            "unit": display["unit"],
            "prefix": display.get("prefix", ""),
            "direction": display["direction"],
            "latest_value": latest_pt.value,
            "latest_at": latest_pt.collected_at,
            "latest_meta": latest_pt.meta or {},
            "delta": delta,
            "spark": spark,
        })

    # ── Positioning snapshots (append-only; latest = current view) ──
    positioning = (
        db.query(PositioningSnapshot)
        .filter(PositioningSnapshot.competitor_id == c.id)
        .order_by(PositioningSnapshot.created_at.desc())
        .first()
    )
    positioning_history = (
        db.query(PositioningSnapshot)
        .filter(PositioningSnapshot.competitor_id == c.id)
        .order_by(PositioningSnapshot.created_at.desc())
        .offset(1)
        .limit(10)
        .all()
    )
    positioning_err = request.query_params.get("positioning_err")

    # ── Deep research (Gemini) — latest + history + any in-flight run ──
    research_latest = (
        db.query(DeepResearchReport)
        .filter(DeepResearchReport.competitor_id == c.id)
        .order_by(DeepResearchReport.started_at.desc())
        .first()
    )
    research_history = (
        db.query(DeepResearchReport)
        .filter(DeepResearchReport.competitor_id == c.id)
        .order_by(DeepResearchReport.started_at.desc())
        .offset(1)
        .limit(10)
        .all()
    )
    research_in_flight = (
        research_latest
        if research_latest and research_latest.status in ("queued", "running")
        else None
    )
    research_gemini_key_set = bool(os.environ.get("GEMINI_API_KEY", "").strip())

    return templates.TemplateResponse(request, "competitor_profile.html", {
        "user": user, "c": c, "latest": latest, "history": history,
        "findings": findings, "provider_breakdown": provider_breakdown,
        "momentum": momentum,
        "positioning": positioning,
        "positioning_history": positioning_history,
        "positioning_err": positioning_err,
        "research_latest": research_latest,
        "research_history": research_history,
        "research_in_flight": research_in_flight,
        "research_gemini_key_set": research_gemini_key_set,
    })


def _discover_page_context(db) -> dict:
    """Shared context for Manage Watchlist's Discover panel — loaded both on
    the main page render and by the HTMX status poller."""
    active_run = (
        db.query(Run)
        .filter(Run.kind == "discover_competitors", Run.status == "running")
        .order_by(Run.started_at.desc())
        .first()
    )
    last_run = (
        db.query(Run)
        .filter(Run.kind == "discover_competitors")
        .order_by(Run.started_at.desc())
        .first()
    )
    suggested = (
        db.query(CompetitorCandidate)
        .filter(CompetitorCandidate.status == "suggested")
        .order_by(CompetitorCandidate.created_at.desc())
        .all()
    )
    dismissed = (
        db.query(CompetitorCandidate)
        .filter(CompetitorCandidate.status == "dismissed")
        .order_by(CompetitorCandidate.dismissed_at.desc())
        .limit(50)
        .all()
    )
    anthropic_key_set = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    tavily_key_set = bool(os.environ.get("TAVILY_API_KEY", "").strip())
    return {
        "discover_active_run": active_run,
        "discover_last_run": last_run,
        "discover_suggested": suggested,
        "discover_dismissed": dismissed,
        "discover_anthropic_key_set": anthropic_key_set,
        "discover_tavily_key_set": tavily_key_set,
    }


@router.get("/admin/competitors", response_class=HTMLResponse)
def admin_competitors(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    active = db.query(Competitor).filter(Competitor.active == True).order_by(Competitor.name).all()
    inactive = db.query(Competitor).filter(Competitor.active == False).order_by(Competitor.name).all()
    ctx = {"user": user, "active": active, "inactive": inactive}
    ctx.update(_discover_page_context(db))
    return templates.TemplateResponse(request, "admin_competitors.html", ctx)


@router.get("/partials/discover_status", response_class=HTMLResponse)
def partial_discover_status(request: Request, db: Session = Depends(get_db),
                            _=Depends(get_current_user)):
    """HTMX polls this while a discovery run is active. Returns the status
    card partial, which self-removes the poller on terminal states by
    swapping in the full panel (no-op: the next poll just sees no active
    run and renders the idle form)."""
    ctx = _discover_page_context(db)
    return templates.TemplateResponse(request, "_discover_status.html", ctx)


@router.get("/admin/competitors/new", response_class=HTMLResponse)
def admin_competitor_new(request: Request, candidate_id: int | None = None,
                         db: Session = Depends(get_db),
                         user=Depends(get_current_user)):
    """New competitor form. When `candidate_id` is supplied, the form pre-
    fills Name + Homepage from the candidate row and auto-fires the
    autofill agent on load. A save flips the candidate to 'adopted' and
    links it to the new Competitor.id (wired in the save handler)."""
    candidate = None
    if candidate_id is not None:
        candidate = db.get(CompetitorCandidate, candidate_id)
        if candidate is None or candidate.status == "adopted":
            candidate = None
    return templates.TemplateResponse(request, "admin_competitor_edit.html", {
        "user": user, "c": None, "candidate": candidate,
    })


@router.get("/admin/competitors/{competitor_id}/edit", response_class=HTMLResponse)
def admin_competitor_edit(competitor_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "admin_competitor_edit.html", {
        "user": user, "c": c,
    })


@router.get("/settings", response_class=HTMLResponse)
def settings_home(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(request, "settings_home.html", {"user": user})


@router.get("/settings/keys", response_class=HTMLResponse)
def settings_keys(request: Request, user=Depends(get_current_user)):
    from . import env_keys as _env_keys
    return templates.TemplateResponse(request, "settings_keys.html", {
        "user": user, "keys": _env_keys.status(),
    })


@router.get("/settings/usage", response_class=HTMLResponse)
def settings_usage(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return admin_usage(request, db, user)


@router.get("/settings/providers", response_class=HTMLResponse)
def settings_providers(request: Request, user=Depends(get_current_user)):
    import json
    from . import search_providers
    from .config_sync import CONFIG_PATH  # absolute path, resolved once at import
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[settings/providers] failed to read config: {e}")
        cfg = {}
    fetcher_cfg = cfg.get("fetcher") or {}
    return templates.TemplateResponse(request, "settings_providers.html", {
        "user": user,
        "providers": search_providers.provider_status(cfg),
        "zenrows_primary":    bool(fetcher_cfg.get("zenrows_primary", True)),
        "zenrows_key_set":    bool(os.environ.get("ZENROWS_API_KEY", "")),
        "scrapingbee_primary": bool(fetcher_cfg.get("scrapingbee_primary", False)),
        "scrapingbee_key_set": bool(os.environ.get("SCRAPINGBEE_API_KEY", "")),
    })


@router.get("/admin/skills", response_class=HTMLResponse)
def admin_skills(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    from .models import Skill as SkillModel
    from .skills import KNOWN_SKILLS
    rows = []
    for name, (_fname, desc) in KNOWN_SKILLS.items():
        active = (
            db.query(SkillModel)
            .filter(SkillModel.name == name, SkillModel.active == True)
            .order_by(SkillModel.version.desc())
            .first()
        )
        total = db.query(SkillModel).filter(SkillModel.name == name).count()
        rows.append({
            "name": name, "description": desc,
            "active": active, "total_versions": total,
        })
    return templates.TemplateResponse(request, "admin_skills.html", {
        "user": user, "rows": rows,
    })


@router.get("/admin/skills/{name}", response_class=HTMLResponse)
def admin_skill_edit(name: str, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    from .models import Skill as SkillModel
    from .skills import KNOWN_SKILLS, load_active
    if name not in KNOWN_SKILLS:
        raise HTTPException(404)
    active = (
        db.query(SkillModel)
        .filter(SkillModel.name == name, SkillModel.active == True)
        .order_by(SkillModel.version.desc())
        .first()
    )
    history = (
        db.query(SkillModel)
        .filter(SkillModel.name == name)
        .order_by(SkillModel.version.desc())
        .all()
    )
    return templates.TemplateResponse(request, "admin_skill_edit.html", {
        "user": user,
        "name": name,
        "description": KNOWN_SKILLS[name][1],
        "active": active,
        "history": history,
        "body_md": active.body_md if active else load_active(name),
    })


@router.get("/admin/usage", response_class=HTMLResponse)
def admin_usage(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    now = datetime.utcnow()

    def _sum(since):
        cost, it, ot, cr, calls = db.query(
            _func.coalesce(_func.sum(UsageEvent.cost_usd), 0.0),
            _func.coalesce(_func.sum(UsageEvent.input_tokens), 0),
            _func.coalesce(_func.sum(UsageEvent.output_tokens), 0),
            _func.coalesce(_func.sum(UsageEvent.credits), 0),
            _func.count(UsageEvent.id),
        ).filter(UsageEvent.ts >= since).one()
        return {"cost": float(cost), "input": int(it), "output": int(ot),
                "credits": int(cr), "calls": int(calls)}

    totals = {
        "day":   _sum(now - timedelta(days=1)),
        "week":  _sum(now - timedelta(days=7)),
        "month": _sum(now - timedelta(days=30)),
    }

    by_model = (
        db.query(
            UsageEvent.provider, UsageEvent.model,
            _func.count(UsageEvent.id),
            _func.coalesce(_func.sum(UsageEvent.input_tokens), 0),
            _func.coalesce(_func.sum(UsageEvent.output_tokens), 0),
            _func.coalesce(_func.sum(UsageEvent.credits), 0),
            _func.coalesce(_func.sum(UsageEvent.cost_usd), 0.0),
        )
        .filter(UsageEvent.ts >= now - timedelta(days=30))
        .group_by(UsageEvent.provider, UsageEvent.model)
        .order_by(_func.sum(UsageEvent.cost_usd).desc())
        .all()
    )

    recent = (
        db.query(UsageEvent).order_by(UsageEvent.id.desc()).limit(30).all()
    )

    return templates.TemplateResponse(request, "settings_usage.html", {
        "user": user,
        "totals": totals,
        "by_model": by_model,
        "recent": recent,
    })


@router.get("/runs", response_class=HTMLResponse)
def runs_index(
    request: Request,
    page: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    page_size = 50
    page = max(1, page)
    total = db.query(func.count(Run.id)).scalar() or 0
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    rows = (
        db.query(Run)
        .order_by(Run.started_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    is_running = db.query(Run).filter(Run.status == "running").first() is not None
    return templates.TemplateResponse(request, "runs_index.html", {
        "user": user,
        "runs": rows,
        "is_running": is_running,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "page_size": page_size,
    })


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "run.html", {
        "user": user, "run": run, "events": run.events,
    })


@router.get("/market", response_class=HTMLResponse)
def market_index(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    # Digest tab: show the latest Claude digest inline and collapse older
    # ones into a disclosure (same shape as the synthesis tab). A list of
    # 7-day-old digests nobody re-reads isn't earning its pixels.
    latest_digest = (
        db.query(Report)
        .order_by(Report.created_at.desc())
        .first()
    )
    digest_history = (
        db.query(Report)
        .order_by(Report.created_at.desc())
        .offset(1)
        .limit(20)
        .all()
    )

    latest_synthesis = (
        db.query(MarketSynthesisReport)
        .order_by(MarketSynthesisReport.started_at.desc())
        .first()
    )
    synthesis_history = (
        db.query(MarketSynthesisReport)
        .filter(MarketSynthesisReport.status == "ready")
        .order_by(MarketSynthesisReport.started_at.desc())
        .offset(1)
        .limit(20)
        .all()
    )
    # Second argument tells the status partial whether to activate the
    # HTMX poller — only when the latest row is still working.
    synthesis_poll = (
        latest_synthesis is not None
        and latest_synthesis.status in ("queued", "running")
    )
    return templates.TemplateResponse(request, "market_index.html", {
        "user": user,
        "digest": latest_digest,
        "digest_history": digest_history,
        "synthesis": latest_synthesis,
        "synthesis_history": synthesis_history,
        "synthesis_poll": synthesis_poll,
        "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
    })


@router.get("/market/synthesis/{synthesis_id}", response_class=HTMLResponse)
def market_synthesis_detail(
    synthesis_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    synthesis = db.get(MarketSynthesisReport, synthesis_id)
    if not synthesis:
        raise HTTPException(404)
    history = (
        db.query(MarketSynthesisReport)
        .filter(
            MarketSynthesisReport.id != synthesis_id,
            MarketSynthesisReport.status == "ready",
        )
        .order_by(MarketSynthesisReport.started_at.desc())
        .limit(20)
        .all()
    )
    return templates.TemplateResponse(request, "market_synthesis_detail.html", {
        "user": user,
        "synthesis": synthesis,
        "history": history,
        "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
    })


@router.get("/market/{report_id}", response_class=HTMLResponse)
def market_detail(report_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    report = db.get(Report, report_id)
    if not report:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "market_detail.html", {
        "user": user, "report": report,
    })


@router.get("/partials/synthesis_status", response_class=HTMLResponse)
def partial_synthesis_status(
    request: Request,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """HTMX polls this while a synthesis run is in flight. Returns the
    status card markup; when the latest row is terminal (`ready`/`failed`),
    returns the ready/failed state card and drops the poll trigger so
    HTMX stops calling. There is always at most one synthesis in flight
    globally, so this endpoint is a singleton — no id in the path."""
    latest = (
        db.query(MarketSynthesisReport)
        .order_by(MarketSynthesisReport.started_at.desc())
        .first()
    )
    return templates.TemplateResponse(request, "_synthesis_status.html", {
        "s": latest,
        "poll": latest is not None and latest.status in ("queued", "running"),
        "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
    })


@router.get("/partials/synthesis_run_form", response_class=HTMLResponse)
def partial_synthesis_run_form(
    request: Request,
    window_days: int = 30,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Pre-run settings form for Market Deep Synthesis. Returned as an
    HTMX fragment when the operator clicks any "Run synthesis" button;
    swapped into #synthesis-status in place of the status card. Shows
    the exact brief that compose_brief() produced (editable) + the agent
    variant + resolved API settings.

    `window_days` is read off the querystring so the "Regenerate brief"
    button can re-fetch the form with a different lookback window
    without a full page reload.

    Canceling the form re-fetches /partials/synthesis_status, which
    restores the original status card.
    """
    from datetime import timedelta
    from .jobs import (
        MARKET_SYNTHESIS_TIMEOUT_S,
        _MARKET_SYNTHESIS_POLL_S,
        current_synthesis_load,
    )
    from .market_synthesis import compose_brief
    from .adapters.gemini_research import AGENT_PREVIEW, AGENT_MAX
    from .routes.runs import _MARKET_SYNTHESIS_COOLDOWN_S

    window = max(1, min(int(window_days), 365))
    brief, inputs_meta = compose_brief(db, window_days=window)

    latest_ready = (
        db.query(MarketSynthesisReport)
        .filter(MarketSynthesisReport.status == "ready")
        .order_by(MarketSynthesisReport.started_at.desc())
        .first()
    )
    cooldown_note: str | None = None
    if latest_ready and latest_ready.started_at:
        age = (datetime.utcnow() - latest_ready.started_at).total_seconds()
        if age < _MARKET_SYNTHESIS_COOLDOWN_S:
            mins = int(age // 60)
            cooldown_note = (
                f"Latest synthesis is only {mins} min old"
                if mins < 60
                else f"Latest synthesis is only {mins // 60}h {mins % 60}m old"
            )

    api = {
        "model_preview": AGENT_PREVIEW,
        "model_max": AGENT_MAX,
        "timeout_min": MARKET_SYNTHESIS_TIMEOUT_S // 60,
        "poll_s": _MARKET_SYNTHESIS_POLL_S,
        "in_flight": current_synthesis_load(db),
        "key_set": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
    }
    return templates.TemplateResponse(request, "_synthesis_run_form.html", {
        "brief": brief,
        "inputs_meta": inputs_meta,
        "default_agent": "preview",
        "window_days": window,
        "cooldown_note": cooldown_note,
        "api": api,
    })


@router.get("/partials/status_bar", response_class=HTMLResponse)
def partial_status_bar(request: Request, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Global footer status — shown on every page via base.html. Three states:
    running, recently-finished (30s), recently-errored (60s). Else: empty."""
    running = (
        db.query(Run)
        .filter(Run.status.in_(["running", "cancelling"]))
        .order_by(Run.started_at.desc())
        .first()
    )
    if running:
        events = (
            db.query(_func.count(Run.events.property.mapper.class_.id))
            if False else None
        )
        from .models import RunEvent
        ev_count = db.query(_func.count(RunEvent.id)).filter(RunEvent.run_id == running.id).scalar() or 0
        return templates.TemplateResponse(request, "_status_bar.html", {
            "state": "running", "run": running, "event_count": ev_count,
        })

    latest = db.query(Run).order_by(Run.started_at.desc()).first()
    if latest and latest.finished_at:
        age = (datetime.utcnow() - latest.finished_at).total_seconds()
        if latest.status == "ok" and age < 30:
            return templates.TemplateResponse(request, "_status_bar.html", {
                "state": "done", "run": latest, "event_count": 0,
            })
        if latest.status == "error" and age < 60:
            return templates.TemplateResponse(request, "_status_bar.html", {
                "state": "error", "run": latest, "event_count": 0,
            })
    return HTMLResponse("")


@router.get("/partials/findings-volume", response_class=HTMLResponse)
def partial_findings_volume(
    request: Request,
    mode: str = "type",
    days: int = 30,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Daily stacked-bar chart of established findings. Two groupings
    (signal_type / competitor); three windows (7/30/90). HTMX swaps
    #findings-chart-slot with this fragment."""
    from .dashboard_chart import (
        ALLOWED_DAYS,
        SIGNAL_TYPE_COLORS,
        build_findings_volume,
        stable_color_for_competitor,
        OTHER_LABEL,
        OTHER_COLOR,
    )
    if mode not in ("type", "competitor"):
        raise HTTPException(status_code=400, detail="mode must be 'type' or 'competitor'")
    if days not in ALLOWED_DAYS:
        raise HTTPException(status_code=400, detail=f"days must be one of {ALLOWED_DAYS}")

    chart = build_findings_volume(db, mode, days)  # type: ignore[arg-type]

    # Flat key→color map for the template — avoids Jinja branching per rect.
    segment_color: dict[str, str] = {s.key: s.color for s in chart.segments}
    # Fallbacks so a key appearing in bars but not in segments (shouldn't happen,
    # but defensive) still renders.
    if mode == "type":
        for k, v in SIGNAL_TYPE_COLORS.items():
            segment_color.setdefault(k, v)
    else:
        segment_color.setdefault(OTHER_LABEL, OTHER_COLOR)

    return templates.TemplateResponse(request, "_findings_volume.html", {
        "chart": chart,
        "segment_color": segment_color,
    })


@router.get("/partials/status", response_class=HTMLResponse)
def partial_status(request: Request, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """HTMX polls this fragment every 10s to keep the dashboard header live."""
    last_run = db.query(Run).order_by(Run.started_at.desc()).first()
    is_running = db.query(Run).filter(Run.status == "running").first() is not None
    return templates.TemplateResponse(request, "_status.html", {
        "last_run": last_run,
        "next_run_at": scheduler.next_run_at("daily_scan"),
        "is_running": is_running,
    })


@router.get("/partials/live_run", response_class=HTMLResponse)
def partial_live_run(request: Request, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """HTMX polls this every ~2s while a run is in flight.
    Returns empty when nothing is running, which causes the panel to vanish."""
    from .models import RunEvent
    run = (
        db.query(Run)
        .filter(Run.status.in_(["running", "cancelling"]))
        .order_by(Run.started_at.desc())
        .first()
    )
    if not run:
        return HTMLResponse("")
    events = (
        db.query(RunEvent)
        .filter(RunEvent.run_id == run.id)
        .order_by(RunEvent.id.desc())
        .limit(60)
        .all()
    )
    events = list(reversed(events))
    return templates.TemplateResponse(request, "_live_run.html", {
        "run": run,
        "events": events,
    })


@router.get("/partials/research_status/{competitor_id}", response_class=HTMLResponse)
def partial_research_status(
    competitor_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """HTMX polls this while a deep-research run is in flight. Returns the
    status card markup; when the latest row is terminal (`ready` / `failed`),
    returns the report card so HTMX swaps in the finished view in place.
    Stops polling when the response HTML has no hx-trigger (template decides
    based on the status)."""
    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)
    latest = (
        db.query(DeepResearchReport)
        .filter(DeepResearchReport.competitor_id == competitor_id)
        .order_by(DeepResearchReport.started_at.desc())
        .first()
    )
    return templates.TemplateResponse(request, "_research_status.html", {
        "c": c,
        "r": latest,
        "poll": latest is not None and latest.status in ("queued", "running"),
        "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
    })


@router.get("/partials/research_run_form/{competitor_id}", response_class=HTMLResponse)
def partial_research_run_form(
    competitor_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Pre-run settings form for Deep Research. Returned as an HTMX fragment
    when the operator clicks any "Run" button; swapped into #research-status
    in place of the status card. Shows the exact brief that will be sent to
    Gemini (editable), the agent variant, and resolved API settings.

    Canceling the form re-fetches /partials/research_status/{id}, which
    restores the original status card without a page reload.
    """
    import json
    from .jobs import (
        DEEP_RESEARCH_MAX_CONCURRENT,
        DEEP_RESEARCH_TIMEOUT_S,
        _DEEP_RESEARCH_POLL_S,
        current_research_load,
        _build_research_brief,
    )
    from .adapters.gemini_research import AGENT_PREVIEW, AGENT_MAX

    c = db.get(Competitor, competitor_id)
    if not c:
        raise HTTPException(404)

    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    brief = _build_research_brief(c, cfg)

    # Default the agent selector to whatever the latest run used, falling
    # back to preview for a first run. Operator-friendly: re-running after
    # a max dossier keeps max selected unless they click down.
    latest = (
        db.query(DeepResearchReport)
        .filter(DeepResearchReport.competitor_id == competitor_id)
        .order_by(DeepResearchReport.started_at.desc())
        .first()
    )
    default_agent = (latest.agent if latest and latest.agent in ("preview", "max") else "preview")

    api = {
        "model_preview":   AGENT_PREVIEW,
        "model_max":       AGENT_MAX,
        "timeout_min":     max(1, DEEP_RESEARCH_TIMEOUT_S // 60),
        "poll_s":          _DEEP_RESEARCH_POLL_S,
        "concurrency_cap": DEEP_RESEARCH_MAX_CONCURRENT,
        "in_flight":       current_research_load(db),
        "key_set":         bool(os.environ.get("GEMINI_API_KEY", "").strip()),
    }

    return templates.TemplateResponse(request, "_research_run_form.html", {
        "c": c,
        "brief": brief,
        "default_agent": default_agent,
        "api": api,
    })


@router.get("/partials/run_events/{run_id}", response_class=HTMLResponse)
def partial_run_events(
    run_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Events panel for a specific run, used by the run detail page.
    Polls itself every 2s while the run is still 'running', then stops
    (the returned HTML omits the hx-trigger when the run is finished)."""
    from .models import RunEvent
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(404, "run not found")
    events = (
        db.query(RunEvent)
        .filter(RunEvent.run_id == run.id)
        .order_by(RunEvent.id.asc())
        .all()
    )
    return templates.TemplateResponse(request, "_run_events.html", {
        "run": run,
        "events": events,
        "is_running": run.status == "running",
    })


# ─── Stream ───────────────────────────────────────────────────────────────
# Server-rendered feed for typed signals. Matches the /api/findings JSON
# shape but renders HTML partials for HTMX swaps (filter change → list
# reload; pin/dismiss → single card reload).

def _parse_stream_filters(params: dict) -> dict:
    """Read query params into a normalized filter dict. Centralised so the
    full-page GET, the list partial, and saved-filter load all produce the
    same filter state."""
    raw_types = params.getlist("signal_types") if hasattr(params, "getlist") else params.get("signal_types", [])
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    try:
        min_mat = float(params.get("min_materiality", "") or 0.0) or None
    except ValueError:
        min_mat = None
    try:
        since_days = int(params.get("since_days") or 0) or None
    except ValueError:
        since_days = None
    try:
        window = max(0, int(params.get("window") or 0))
    except ValueError:
        window = 0
    # `downweight_stale` defaults on: pushes findings with published_at older
    # than a year to the bottom of the order (still shown). The template
    # emits a hidden "0" in front of the checkbox so form submissions always
    # include the key when unchecked. When we're parsing a saved-filter dict
    # (not a form), absence of the key means the spec predates this flag —
    # fall back to the default.
    is_form = hasattr(params, "getlist")
    ds_vals = params.getlist("downweight_stale") if is_form else [params.get("downweight_stale")]
    ds_vals = [v for v in ds_vals if v is not None and v != ""]
    if ds_vals:
        last = ds_vals[-1]
        # QueryParams sends strings; saved-filter dicts store native bools.
        downweight_stale = last is True or last == "1"
    elif is_form and len(params) > 0:
        downweight_stale = False  # form submitted without the key → unchecked
    else:
        downweight_stale = True  # bare URL or legacy saved spec → default on
    return {
        "competitor": (params.get("competitor") or "").strip() or None,
        "signal_types": [t for t in raw_types if t],
        "min_materiality": min_mat,
        "since_days": since_days,
        "include_dismissed": (params.get("include_dismissed") or "") == "1",
        "downweight_stale": downweight_stale,
        "window": window,
    }


def _build_logo_map(db, findings) -> dict[str, str]:
    """Map competitor name → cached logo URL for the findings on screen.

    Only emits an entry when the logo is already on disk under /logos —
    Apistemic fetches happen server-side at save time + on boot, never in
    the render path. Missing logos silently fall through so the card just
    renders without one.
    """
    if not findings:
        return {}
    names = {f.competitor for f in findings if f.competitor}
    if not names:
        return {}
    from . import logos as _logos
    out: dict[str, str] = {}
    for c in db.query(Competitor).filter(Competitor.name.in_(names)).all():
        url = _logos.logo_url(c.homepage_domain)
        if url:
            out[c.name] = url
    return out


# Stream paging: recall within a 30-day window and rank within it. When the
# user exhausts a window, the "load previous 30 days" button shifts them to
# the next older window. The `Since` dropdown, when set, is an explicit scope
# and overrides windowing (no paging).
STREAM_WINDOW_DAYS = 30
# Safety cap — one window of findings shouldn't realistically exceed this.
# Bounds memory if someone runs the app without filters on a large DB.
STREAM_SAFETY_CAP = 500


def _view_counts_for_user(db, user_id: int, finding_ids: list[int]) -> dict[int, int]:
    """Per-user lifetime `view`-event count keyed by finding_id.

    Powers the "Viewed N×" indicator on stream cards. Counts every `view`
    event ever logged by this user for each finding — no time window. The
    in-DB de-dup (5-min window inside post_events_batch) means each row
    is roughly one distinct page-load impression, which is what users
    expect when reading "viewed 3 times".
    """
    if not finding_ids:
        return {}
    rows = (
        db.query(
            UserSignalEvent.finding_id,
            func.count(UserSignalEvent.id),
        )
        .filter(
            UserSignalEvent.user_id == user_id,
            UserSignalEvent.event_type == "view",
            UserSignalEvent.finding_id.in_(finding_ids),
        )
        .group_by(UserSignalEvent.finding_id)
        .all()
    )
    return {fid: int(n) for fid, n in rows if fid is not None}


def _stream_query(db, user, filters):
    """Build + run the stream query.

    Returns (findings, view_by_finding_id, view_counts, has_more). `has_more`
    is True when windowing is active and there's at least one Finding older
    than the current window — i.e., clicking "load more" would reach real data.
    """
    from sqlalchemy import and_, or_, case
    q = db.query(Finding)
    if filters["competitor"]:
        q = q.filter(Finding.competitor == filters["competitor"])
    if filters["signal_types"]:
        q = q.filter(Finding.signal_type.in_(filters["signal_types"]))
    if filters["min_materiality"] is not None:
        q = q.filter(Finding.materiality >= filters["min_materiality"])

    now = datetime.utcnow()
    has_more = False
    if filters["since_days"]:
        # Explicit date scope — no windowing, no paging.
        cutoff = now - timedelta(days=filters["since_days"])
        q = q.filter(Finding.created_at >= cutoff)
    else:
        window = filters.get("window", 0)
        window_upper = now - timedelta(days=window * STREAM_WINDOW_DAYS)
        window_lower = now - timedelta(days=(window + 1) * STREAM_WINDOW_DAYS)
        q = q.filter(Finding.created_at >= window_lower,
                     Finding.created_at < window_upper)
        # Cheap check — does *any* row exist older than this window? Ignores
        # other filters (competitor/types/etc.); false positives just mean a
        # user sees an empty older window, which is self-explanatory.
        has_more = (
            db.query(Finding.id)
            .filter(Finding.created_at < window_lower)
            .limit(1)
            .first()
            is not None
        )

    # View-state filters: always exclude snoozed-active rows; exclude
    # dismissed unless the user explicitly asked to see them.
    q = q.outerjoin(
        SignalView,
        and_(SignalView.finding_id == Finding.id, SignalView.user_id == user.id),
    )
    if not filters["include_dismissed"]:
        q = q.filter(or_(SignalView.state.is_(None), SignalView.state != "dismissed"))
    q = q.filter(or_(
        SignalView.state.is_(None),
        SignalView.state != "snoozed",
        SignalView.snoozed_until.is_(None),
        SignalView.snoozed_until < now,
    ))

    # Effective date: the source's publish date if we captured one, else our
    # fetch timestamp. Scrape-time can lag the real publish by years (SERP
    # surfacing an old article, backfills), which otherwise makes stale
    # content look brand-new in the stream.
    effective_date = func.coalesce(Finding.published_at, Finding.created_at)

    if filters.get("downweight_stale"):
        # Stale = published over a year ago. NULL published_at is treated as
        # fresh so we don't penalise signals where the source didn't expose a
        # date (many careers / VoC hits). Sorted 0-before-1 pushes stale rows
        # to the bottom while keeping effective date as the within-group order.
        one_year_ago = now - timedelta(days=365)
        stale_flag = case(
            (and_(Finding.published_at.isnot(None), Finding.published_at < one_year_ago), 1),
            else_=0,
        )
        q = q.order_by(stale_flag.asc(), effective_date.desc())
    else:
        q = q.order_by(effective_date.desc())

    findings = q.limit(STREAM_SAFETY_CAP).all()

    # Per-user prior-exposure counts (docs/ranker/07-seen-decay.md). One
    # aggregate query → dict[finding_id, n_prior_views]. `view` fires once
    # per card per page-load, so each event ≈ one prior stream-load where
    # the user had this card on screen. The trailing-EXCLUDE_MINUTES window
    # is the "current session" carve-out — views inside it don't count, so
    # scrolling doesn't reshuffle what's in front of you right now.
    from .ranker import config as _rcfg
    seen_count_by_id: dict[int, int] = {}
    if findings:
        ids = [f.id for f in findings]
        cutoff = now - timedelta(minutes=_rcfg.STANDIN_SEEN_DECAY_EXCLUDE_MINUTES)
        rows = (
            db.query(
                UserSignalEvent.finding_id,
                func.count(UserSignalEvent.id),
            )
            .filter(
                UserSignalEvent.user_id == user.id,
                UserSignalEvent.event_type.in_(("view", "open")),
                UserSignalEvent.finding_id.in_(ids),
                UserSignalEvent.ts < cutoff,
            )
            .group_by(UserSignalEvent.finding_id)
            .all()
        )
        seen_count_by_id = {fid: int(n) for fid, n in rows if fid is not None}

    # Cluster near-duplicates and apply MMR diversity to the top slots
    # (docs/ranker/06-cluster-diversity.md). Pure post-processing — DB
    # query shape is untouched. `lead_findings()` stamps `_cluster_size`
    # on each returned Finding so the template can render the "+N more"
    # chip without restructuring.
    cards = _present_clusters(findings, now=now, seen_count_by_id=seen_count_by_id)
    findings = _lead_findings(cards)

    views: dict[int, SignalView] = {}
    if findings:
        ids = [f.id for f in findings]
        for v in db.query(SignalView).filter(
            SignalView.user_id == user.id,
            SignalView.finding_id.in_(ids),
        ).all():
            views[v.finding_id] = v
    view_counts = _view_counts_for_user(db, user.id, [f.id for f in findings])
    return findings, views, view_counts, has_more


@router.get("/stream", response_class=HTMLResponse)
def stream_page(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    # Auto-apply the user's default filter only when the URL is bare —
    # any query param (even an empty submission from the form) means the
    # user is steering and should override the default.
    active_filter_id: int | None = None
    if not request.query_params and user.default_filter_id:
        default_sf = db.get(SavedFilter, user.default_filter_id)
        filters = _parse_stream_filters(default_sf.spec) if default_sf else _parse_stream_filters({})
        active_filter_id = user.default_filter_id if default_sf else None
    else:
        filters = _parse_stream_filters(request.query_params)
    findings, views, view_counts, has_more = _stream_query(db, user, filters)
    competitors = [c.name for c in db.query(Competitor).filter(Competitor.active == True).order_by(Competitor.name).all()]
    # Saved filters (own + team-shared) for the dropdown.
    from sqlalchemy import or_ as _or
    saved = (
        db.query(SavedFilter)
        .filter(_or(SavedFilter.owner_id == user.id, SavedFilter.owner_id.is_(None)))
        .order_by(SavedFilter.created_at.desc())
        .all()
    )
    # Fire-and-forget: log `shown` events after the response ships so the
    # page render never waits on the write. The task opens its own DB
    # session because Depends(get_db) closes this one at request end.
    background_tasks.add_task(
        emit_shown_events,
        user.id,
        [f.id for f in findings],
        filter_id=active_filter_id,
    )
    return templates.TemplateResponse(request, "stream.html", {
        "user": user,
        "filters": filters,
        "findings": findings,
        "views": views,
        "view_counts": view_counts,
        "has_more": has_more,
        "competitors": competitors,
        "signal_types": SIGNAL_TYPES,
        "saved_filters": saved,
        "default_filter_id": user.default_filter_id,
        "logos": _build_logo_map(db, findings),
    })


@router.get("/partials/stream_list", response_class=HTMLResponse)
def partial_stream_list(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    filters = _parse_stream_filters(request.query_params)
    findings, views, view_counts, has_more = _stream_query(db, user, filters)
    background_tasks.add_task(
        emit_shown_events,
        user.id,
        [f.id for f in findings],
    )
    return templates.TemplateResponse(request, "_stream_list.html", {
        "findings": findings,
        "views": views,
        "view_counts": view_counts,
        "filters": filters,
        "has_more": has_more,
        "logos": _build_logo_map(db, findings),
    })


@router.post("/partials/stream_view/{finding_id}", response_class=HTMLResponse)
async def partial_stream_view(
    finding_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Upsert SignalView and return the re-rendered card.

    Dismissed cards still render (with a 'dismissed' style) so the user
    sees the action landed; the stream query will omit them on next
    filter reload unless include_dismissed=1 is set.
    """
    form = await request.form()
    state = (form.get("state") or "").strip()
    allowed = {"seen", "pinned", "dismissed", "snoozed"}
    if state not in allowed:
        raise HTTPException(400, f"state must be one of {sorted(allowed)}")
    snoozed_until = None
    if state == "snoozed":
        # Default: snooze for 7 days. A custom value could come via form later.
        snoozed_until = datetime.utcnow() + timedelta(days=7)

    f = db.get(Finding, finding_id)
    if not f:
        raise HTTPException(404, "finding not found")
    existing = (
        db.query(SignalView)
        .filter(SignalView.user_id == user.id, SignalView.finding_id == finding_id)
        .first()
    )
    now = datetime.utcnow()
    if existing:
        existing.state = state
        existing.snoozed_until = snoozed_until
        existing.updated_at = now
        v = existing
    else:
        v = SignalView(
            user_id=user.id,
            finding_id=finding_id,
            state=state,
            snoozed_until=snoozed_until,
            updated_at=now,
        )
        db.add(v)
    db.commit()
    view_counts = _view_counts_for_user(db, user.id, [f.id])
    return templates.TemplateResponse(request, "_stream_card.html", {
        "f": f,
        "view": v,
        "view_count": view_counts.get(f.id, 0),
        "logos": _build_logo_map(db, [f]),
    })


@router.post("/partials/stream_save_filter", response_class=HTMLResponse)
async def partial_stream_save_filter(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Save the current filter state as a SavedFilter. Form-encoded: name +
    the same filter query params the list endpoint takes. Returns the
    updated saved-filter dropdown partial."""
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    # Re-parse filter state from the form body (client posts current filter values)
    filters = _parse_stream_filters(form)
    spec = {
        "competitor": filters["competitor"],
        "signal_types": filters["signal_types"],
        "min_materiality": filters["min_materiality"],
        "since_days": filters["since_days"],
        "downweight_stale": filters["downweight_stale"],
    }
    visibility = "team" if (form.get("visibility") == "team") else "private"
    row = SavedFilter(
        owner_id=None if visibility == "team" else user.id,
        name=name,
        spec={k: v for k, v in spec.items() if v not in (None, [], "")},
        visibility=visibility,
    )
    db.add(row)
    db.commit()
    from sqlalchemy import or_ as _or
    saved = (
        db.query(SavedFilter)
        .filter(_or(SavedFilter.owner_id == user.id, SavedFilter.owner_id.is_(None)))
        .order_by(SavedFilter.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(request, "_stream_saved_filters.html", {
        "saved_filters": saved,
        "default_filter_id": user.default_filter_id,
    })


@router.post("/partials/stream_toggle_default/{filter_id}", response_class=HTMLResponse)
async def partial_stream_toggle_default(
    filter_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Toggle a saved filter as the user's default. Returns the refreshed
    saved-filter list so the star icon updates in place."""
    sf = db.get(SavedFilter, filter_id)
    if not sf:
        raise HTTPException(404, "filter not found")
    # Toggle: clearing happens by clicking the currently-default star again.
    user.default_filter_id = None if user.default_filter_id == filter_id else filter_id
    db.commit()
    from sqlalchemy import or_ as _or
    saved = (
        db.query(SavedFilter)
        .filter(_or(SavedFilter.owner_id == user.id, SavedFilter.owner_id.is_(None)))
        .order_by(SavedFilter.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(request, "_stream_saved_filters.html", {
        "saved_filters": saved,
        "default_filter_id": user.default_filter_id,
    })


@router.post("/partials/stream_delete_filter/{filter_id}", response_class=HTMLResponse)
async def partial_stream_delete_filter(
    filter_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Delete a saved filter and return the refreshed saved-filter list.

    Permissions mirror the REST DELETE: users can delete their own private
    filters; team filters (owner_id NULL) are admin-only so a single user
    can't quietly remove a saved view everyone relies on."""
    sf = db.get(SavedFilter, filter_id)
    if not sf:
        raise HTTPException(404, "filter not found")
    if sf.owner_id is None and user.role != "admin":
        raise HTTPException(403, "only admins can delete team filters")
    if sf.owner_id and sf.owner_id != user.id:
        raise HTTPException(403, "not your filter")
    # Clear the user's default pointer if this was it — FK is SET NULL,
    # but we commit the user side first for a clean audit trail.
    if user.default_filter_id == filter_id:
        user.default_filter_id = None
    db.delete(sf)
    db.commit()
    from sqlalchemy import or_ as _or
    saved = (
        db.query(SavedFilter)
        .filter(_or(SavedFilter.owner_id == user.id, SavedFilter.owner_id.is_(None)))
        .order_by(SavedFilter.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(request, "_stream_saved_filters.html", {
        "saved_filters": saved,
        "default_filter_id": user.default_filter_id,
    })
