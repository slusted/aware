"""Curated tool catalog for the chat agent.

Each tool wraps an existing service or query — we never auto-expose
FastAPI routes, because the route surface is too large and leaks
admin-only endpoints. Read tools execute inline; write tools set
``requires_confirmation=True`` on the dispatcher so the UI surfaces a
Confirm/Cancel card before the handler fires.

Each handler returns JSON-serialisable data the model can read. Output
is capped at ``MAX_OUTPUT_CHARS`` per tool call — anything bigger is
summarised down to ids + counts so the model can re-call with a tighter
filter rather than blow the context window.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import skills as skills_module
from ..models import (
    ChatSession,
    Competitor,
    CompetitorReport,
    ContextBrief,
    DeepResearchReport,
    Finding,
    MarketSynthesisReport,
    Report,
    Run,
    User,
)


MAX_OUTPUT_CHARS = 8000


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]
    requires_role: str = "viewer"
    requires_confirmation: bool = False
    confirmation_summary: Callable[[dict], str] | None = None


# ----- helpers ---------------------------------------------------------------


def _truncate_output(payload: Any) -> tuple[Any, bool]:
    """Cap a tool payload at MAX_OUTPUT_CHARS of serialised JSON. When
    over the cap, return a stub describing how to narrow the query.
    Returns (payload, truncated_bool)."""
    serialised = json.dumps(payload, default=str)
    if len(serialised) <= MAX_OUTPUT_CHARS:
        return payload, False
    if isinstance(payload, dict) and "results" in payload and isinstance(payload["results"], list):
        kept: list = []
        size = len(json.dumps({"results": [], "truncated": True, "total": payload.get("total", 0)}))
        for row in payload["results"]:
            row_size = len(json.dumps(row, default=str)) + 1
            if size + row_size > MAX_OUTPUT_CHARS:
                break
            kept.append(row)
            size += row_size
        return {
            "results": kept,
            "total": payload.get("total", len(payload["results"])),
            "returned": len(kept),
            "truncated": True,
            "hint": "Output truncated — call again with narrower filters (smaller since_days, specific competitor, etc.).",
        }, True
    truncated_text = serialised[:MAX_OUTPUT_CHARS]
    return {
        "value": truncated_text,
        "truncated": True,
        "hint": "Output truncated — narrow your query.",
    }, True


def _competitor_by_ref(db: Session, ref: str | int) -> Competitor | None:
    """Resolve a competitor by id or by case-insensitive name."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        comp = db.get(Competitor, int(ref))
        if comp:
            return comp
    if isinstance(ref, str):
        return (
            db.query(Competitor)
            .filter(Competitor.name.ilike(ref.strip()))
            .first()
        )
    return None


def _format_competitor(comp: Competitor) -> dict:
    return {
        "id": comp.id,
        "name": comp.name,
        "category": comp.category,
        "threat_angle": comp.threat_angle,
        "active": comp.active,
        "homepage_domain": comp.homepage_domain,
        "last_activity_at": comp.last_activity_at.isoformat() if comp.last_activity_at else None,
    }


def _format_finding(f: Finding) -> dict:
    return {
        "id": f.id,
        "competitor": f.competitor,
        "signal_type": f.signal_type,
        "title": f.title,
        "summary": f.summary,
        "url": f.url,
        "source": f.source,
        "materiality": f.materiality,
        "digest_threat_level": f.digest_threat_level,
        "published_at": f.published_at.isoformat() if f.published_at else None,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    }


# ----- read handlers ---------------------------------------------------------


def _h_list_competitors(db: Session, user: User, **_: Any) -> dict:
    rows = (
        db.query(Competitor)
        .filter(Competitor.active == True)  # noqa: E712
        .order_by(Competitor.name)
        .all()
    )
    return {"results": [_format_competitor(c) for c in rows], "total": len(rows)}


def _h_get_competitor_profile(db: Session, user: User, *, competitor: str | int, **_: Any) -> dict:
    comp = _competitor_by_ref(db, competitor)
    if not comp:
        return {"error": f"competitor not found: {competitor!r}"}

    since = datetime.utcnow() - timedelta(days=7)
    recent_count = (
        db.query(Finding)
        .filter(Finding.competitor == comp.name, Finding.created_at >= since)
        .count()
    )
    review = (
        db.query(CompetitorReport)
        .filter(CompetitorReport.competitor_id == comp.id)
        .order_by(CompetitorReport.created_at.desc())
        .first()
    )
    review_excerpt = (review.body_md or "")[:1200] if review else None

    out = _format_competitor(comp)
    out.update({
        "findings_last_7d": recent_count,
        "latest_review": (
            {
                "id": review.id,
                "created_at": review.created_at.isoformat(),
                "excerpt": review_excerpt,
            }
            if review
            else None
        ),
    })
    return out


def _h_search_findings(
    db: Session,
    user: User,
    *,
    competitor: str | None = None,
    signal_type: str | None = None,
    since_days: int | None = 7,
    min_materiality: float | None = None,
    limit: int = 20,
    **_: Any,
) -> dict:
    q = db.query(Finding)
    if competitor:
        q = q.filter(Finding.competitor.ilike(competitor))
    if signal_type:
        q = q.filter(Finding.signal_type == signal_type)
    if since_days is not None:
        cutoff = datetime.utcnow() - timedelta(days=max(1, int(since_days)))
        q = q.filter(Finding.created_at >= cutoff)
    if min_materiality is not None:
        q = q.filter(Finding.materiality >= float(min_materiality))

    limit = max(1, min(int(limit), 50))
    rows = (
        q.order_by(Finding.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "results": [_format_finding(r) for r in rows],
        "returned": len(rows),
        "filters": {
            "competitor": competitor,
            "signal_type": signal_type,
            "since_days": since_days,
            "min_materiality": min_materiality,
        },
    }


def _h_get_finding(db: Session, user: User, *, finding_id: int, **_: Any) -> dict:
    f = db.get(Finding, int(finding_id))
    if not f:
        return {"error": f"finding #{finding_id} not found"}
    out = _format_finding(f)
    out["content_excerpt"] = (f.content or "")[:2000]
    out["payload"] = f.payload or {}
    return out


def _h_list_reports(db: Session, user: User, *, limit: int = 10, **_: Any) -> dict:
    limit = max(1, min(int(limit), 50))
    rows = (
        db.query(Report)
        .order_by(Report.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "results": [
            {
                "id": r.id,
                "title": r.title,
                "created_at": r.created_at.isoformat(),
                "run_id": r.run_id,
            }
            for r in rows
        ],
        "returned": len(rows),
    }


def _h_get_report(db: Session, user: User, *, report_id: int, **_: Any) -> dict:
    r = db.get(Report, int(report_id))
    if not r:
        return {"error": f"report #{report_id} not found"}
    return {
        "id": r.id,
        "title": r.title,
        "created_at": r.created_at.isoformat(),
        "body_md": r.body_md,
        "run_id": r.run_id,
    }


def _h_get_latest_market_synthesis(db: Session, user: User, **_: Any) -> dict:
    row = (
        db.query(MarketSynthesisReport)
        .filter(MarketSynthesisReport.status == "ready")
        .order_by(MarketSynthesisReport.started_at.desc())
        .first()
    )
    if not row:
        return {"error": "no completed market synthesis available yet"}
    return {
        "id": row.id,
        "started_at": row.started_at.isoformat(),
        "agent": row.agent,
        "window_days": row.window_days,
        "body_md_excerpt": (row.body_md or "")[:4000],
        "sources_count": len(row.sources or []),
        "inputs_meta": row.inputs_meta or {},
        "url": f"/market/synthesis/{row.id}",
    }


def _h_get_deep_research_report(
    db: Session, user: User, *, competitor: str | int, **_: Any
) -> dict:
    comp = _competitor_by_ref(db, competitor)
    if not comp:
        return {"error": f"competitor not found: {competitor!r}"}
    row = (
        db.query(DeepResearchReport)
        .filter(
            DeepResearchReport.competitor_id == comp.id,
            DeepResearchReport.status == "ready",
        )
        .order_by(DeepResearchReport.started_at.desc())
        .first()
    )
    if not row:
        return {"error": f"no completed deep research for {comp.name}"}
    return {
        "id": row.id,
        "competitor_id": comp.id,
        "competitor_name": comp.name,
        "started_at": row.started_at.isoformat(),
        "agent": row.agent,
        "body_md_excerpt": (row.body_md or "")[:4000],
        "sources_count": len(row.sources or []),
        "url": f"/competitors/{comp.id}#research",
    }


def _h_list_recent_runs(db: Session, user: User, *, limit: int = 10, **_: Any) -> dict:
    limit = max(1, min(int(limit), 50))
    rows = (
        db.query(Run)
        .order_by(Run.started_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "results": [
            {
                "id": r.id,
                "kind": r.kind,
                "status": r.status,
                "triggered_by": r.triggered_by,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "findings_count": r.findings_count,
                "error": r.error,
            }
            for r in rows
        ],
        "returned": len(rows),
    }


def _h_get_company_brief(db: Session, user: User, **_: Any) -> dict:
    return _latest_context(db, "company")


def _h_get_customer_brief(db: Session, user: User, **_: Any) -> dict:
    return _latest_context(db, "customer")


def _latest_context(db: Session, scope: str) -> dict:
    row = (
        db.query(ContextBrief)
        .filter(ContextBrief.scope == scope)
        .order_by(ContextBrief.created_at.desc())
        .first()
    )
    if not row:
        return {"error": f"no {scope} brief available yet"}
    return {
        "id": row.id,
        "scope": row.scope,
        "created_at": row.created_at.isoformat(),
        "body_md": row.body_md,
    }


def _h_get_skill_body(db: Session, user: User, *, name: str, **_: Any) -> dict:
    if name not in skills_module.KNOWN_SKILLS:
        return {"error": f"unknown skill: {name!r}"}
    body = skills_module.load_active(name)
    return {
        "name": name,
        "description": skills_module.KNOWN_SKILLS[name][1],
        "body_md": body or "(empty)",
    }


# ----- write handlers --------------------------------------------------------


def _h_run_market_digest(
    db: Session, user: User, *, _session: "ChatSession | None" = None, **_: Any
) -> dict:
    """Trigger the same job /api/runs/market-digest enqueues. Returns the
    queued metadata; the actual Run row is created inside the job."""
    in_flight = (
        db.query(Run)
        .filter(Run.status.in_(["running", "cancelling"]))
        .first()
    )
    if in_flight:
        return {
            "error": (
                f"run #{in_flight.id} ({in_flight.kind}) is already in flight — "
                "wait for it to finish before starting another."
            )
        }
    from .. import jobs as _jobs
    import threading

    tag = f"chat:{_session.id}" if _session is not None else f"chat:user-{user.id}"
    threading.Thread(
        target=_jobs.run_market_digest_job,
        args=(tag,),
        name=f"chat-market-digest-{user.id}",
        daemon=True,
    ).start()
    return {"queued": True, "kind": "market_digest", "url": "/runs"}


def _h_run_deep_research(
    db: Session,
    user: User,
    *,
    competitor: str | int,
    agent: str = "preview",
    _session: "ChatSession | None" = None,
    **_: Any,
) -> dict:
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return {"error": "GEMINI_API_KEY is not set. Add it on /settings/keys before running deep research."}
    comp = _competitor_by_ref(db, competitor)
    if not comp:
        return {"error": f"competitor not found: {competitor!r}"}

    from ..jobs import (
        DEEP_RESEARCH_MAX_CONCURRENT,
        current_research_load,
        run_deep_research_job,
        _build_research_brief,
    )

    in_flight = (
        db.query(DeepResearchReport)
        .filter(
            DeepResearchReport.competitor_id == comp.id,
            DeepResearchReport.status.in_(["queued", "running"]),
        )
        .first()
    )
    if in_flight:
        return {
            "error": (
                f"deep research #{in_flight.id} for {comp.name} is already in flight — "
                "wait for it to finish."
            )
        }

    load = current_research_load(db)
    if load >= DEEP_RESEARCH_MAX_CONCURRENT:
        return {
            "error": (
                f"{load} deep-research runs already in flight "
                f"(cap={DEEP_RESEARCH_MAX_CONCURRENT}). Wait for one to finish."
            )
        }

    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as _f:
            cfg = json.load(_f)
    except Exception:
        cfg = {}
    resolved_brief = _build_research_brief(comp, cfg)
    agent_resolved = "max" if (agent or "").lower().strip() == "max" else "preview"

    report = DeepResearchReport(
        competitor_id=comp.id,
        agent=agent_resolved,
        status="queued",
        brief=resolved_brief,
        started_at=datetime.utcnow(),
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    import threading

    tag = f"chat:{_session.id}" if _session is not None else f"chat:user-{user.id}"
    threading.Thread(
        target=run_deep_research_job,
        args=(comp.id, report.id, agent_resolved, tag),
        name=f"chat-deep-research-{comp.id}",
        daemon=True,
    ).start()
    return {
        "queued": True,
        "kind": "deep_research",
        "report_id": report.id,
        "competitor_id": comp.id,
        "competitor_name": comp.name,
        "agent": agent_resolved,
        "url": f"/competitors/{comp.id}#research",
    }


def _h_add_competitor(
    db: Session,
    user: User,
    *,
    name: str,
    **_: Any,
) -> dict:
    """Add a new competitor to the watchlist. Hands the bare name to the
    autofill agent, which does its own search+fetch tool-use loop to
    populate every field, then writes the row. Same path as the New
    Competitor form on /admin/competitors/new."""
    name = (name or "").strip()
    if not name:
        return {"error": "competitor name is required"}

    existing = (
        db.query(Competitor)
        .filter(Competitor.name.ilike(name))
        .first()
    )
    if existing and existing.active:
        return {
            "error": f"competitor {existing.name!r} is already on the watchlist (id={existing.id}).",
            "competitor_id": existing.id,
            "url": f"/competitors/{existing.id}",
        }

    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as _f:
            cfg = json.load(_f)
    except Exception:
        cfg = {}
    company = cfg.get("company", "our company")
    industry = cfg.get("industry", "our industry")

    try:
        from ..competitor_autofill import autofill as _autofill
        result = _autofill(name, company, industry)
    except Exception as e:
        return {"error": f"autofill failed: {type(e).__name__}: {e}"}

    data = result.get("data") or {}

    if existing:
        # Re-activate + merge in any newly-discovered fields rather than
        # creating a duplicate. Same posture the admin form takes.
        existing.active = True
        for field in (
            "category", "threat_angle", "homepage_domain",
            "app_store_id", "play_package", "trends_keyword",
        ):
            v = data.get(field)
            if v and not getattr(existing, field, None):
                setattr(existing, field, v)
        for field in ("keywords", "subreddits", "careers_domains", "newsroom_domains"):
            current = list(getattr(existing, field, None) or [])
            for item in data.get(field) or []:
                if item and item not in current:
                    current.append(item)
            setattr(existing, field, current)
        db.commit()
        return {
            "reactivated": True,
            "competitor_id": existing.id,
            "name": existing.name,
            "category": existing.category,
            "homepage_domain": existing.homepage_domain,
            "url": f"/competitors/{existing.id}",
        }

    comp = Competitor(
        name=name,
        category=data.get("category"),
        threat_angle=data.get("threat_angle"),
        keywords=list(data.get("keywords") or []),
        subreddits=list(data.get("subreddits") or []),
        careers_domains=list(data.get("careers_domains") or []),
        newsroom_domains=list(data.get("newsroom_domains") or []),
        homepage_domain=data.get("homepage_domain"),
        app_store_id=data.get("app_store_id"),
        play_package=data.get("play_package"),
        trends_keyword=data.get("trends_keyword"),
        min_relevance_score=data.get("min_relevance_score"),
        social_score_multiplier=data.get("social_score_multiplier"),
        source="manual",
        active=True,
    )
    db.add(comp)
    db.commit()
    db.refresh(comp)
    return {
        "added": True,
        "competitor_id": comp.id,
        "name": comp.name,
        "category": comp.category,
        "homepage_domain": comp.homepage_domain,
        "threat_angle": comp.threat_angle,
        "keywords": comp.keywords or [],
        "url": f"/competitors/{comp.id}",
    }


def _h_run_market_synthesis(
    db: Session,
    user: User,
    *,
    agent: str = "preview",
    window_days: int = 30,
    _session: "ChatSession | None" = None,
    **_: Any,
) -> dict:
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return {"error": "GEMINI_API_KEY is not set. Add it on /settings/keys before running synthesis."}
    in_flight = (
        db.query(MarketSynthesisReport)
        .filter(MarketSynthesisReport.status.in_(["queued", "running"]))
        .first()
    )
    if in_flight:
        return {"error": f"market synthesis #{in_flight.id} is already in flight — wait for it to finish."}
    agent_resolved = agent if agent in ("preview", "max") else "preview"
    window = max(1, min(int(window_days), 365))
    from .. import jobs as _jobs
    import threading

    tag = f"chat:{_session.id}" if _session is not None else f"chat:user-{user.id}"
    threading.Thread(
        target=_jobs.run_market_synthesis_job,
        args=(tag, agent_resolved, window, None),
        name=f"chat-market-synthesis-{user.id}",
        daemon=True,
    ).start()
    return {
        "queued": True,
        "kind": "market_synthesis",
        "agent": agent_resolved,
        "window_days": window,
        "url": "/market",
    }


# ----- catalog ---------------------------------------------------------------


TOOLS: list[Tool] = [
    Tool(
        name="list_competitors",
        description="List all active competitors on the watchlist with their category and threat angle.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_h_list_competitors,
        requires_role="viewer",
    ),
    Tool(
        name="get_competitor_profile",
        description="Fetch one competitor's profile: roster + 7-day finding count + latest review excerpt. Accepts id or case-insensitive name.",
        input_schema={
            "type": "object",
            "properties": {
                "competitor": {
                    "type": ["string", "integer"],
                    "description": "Competitor id (int) or name (string).",
                },
            },
            "required": ["competitor"],
            "additionalProperties": False,
        },
        handler=_h_get_competitor_profile,
        requires_role="viewer",
    ),
    Tool(
        name="search_findings",
        description="Search recent findings, optionally filtered by competitor, signal_type, materiality, and recency.",
        input_schema={
            "type": "object",
            "properties": {
                "competitor": {"type": "string", "description": "Optional competitor name (case-insensitive exact match)."},
                "signal_type": {
                    "type": "string",
                    "enum": [
                        "funding", "new_hire", "product_launch", "messaging_shift",
                        "price_change", "integration", "voc_mention", "momentum_point",
                        "news", "other",
                    ],
                    "description": "Optional signal-type filter.",
                },
                "since_days": {"type": "integer", "minimum": 1, "maximum": 365, "description": "Lookback window in days. Defaults to 7."},
                "min_materiality": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Optional minimum materiality (0.0–1.0)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Max rows to return. Defaults to 20."},
            },
            "additionalProperties": False,
        },
        handler=_h_search_findings,
        requires_role="viewer",
    ),
    Tool(
        name="get_finding",
        description="Fetch one finding's full row including content excerpt and payload, by id.",
        input_schema={
            "type": "object",
            "properties": {"finding_id": {"type": "integer"}},
            "required": ["finding_id"],
            "additionalProperties": False,
        },
        handler=_h_get_finding,
        requires_role="viewer",
    ),
    Tool(
        name="list_reports",
        description="List the most recent market digest reports.",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "additionalProperties": False,
        },
        handler=_h_list_reports,
        requires_role="viewer",
    ),
    Tool(
        name="get_report",
        description="Fetch the body of one market digest report by id.",
        input_schema={
            "type": "object",
            "properties": {"report_id": {"type": "integer"}},
            "required": ["report_id"],
            "additionalProperties": False,
        },
        handler=_h_get_report,
        requires_role="viewer",
    ),
    Tool(
        name="get_latest_market_synthesis",
        description="Fetch the latest completed cross-competitor market synthesis (Gemini Deep Research output).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_h_get_latest_market_synthesis,
        requires_role="viewer",
    ),
    Tool(
        name="get_deep_research_report",
        description="Fetch the latest completed Gemini Deep Research dossier for one competitor.",
        input_schema={
            "type": "object",
            "properties": {"competitor": {"type": ["string", "integer"]}},
            "required": ["competitor"],
            "additionalProperties": False,
        },
        handler=_h_get_deep_research_report,
        requires_role="viewer",
    ),
    Tool(
        name="list_recent_runs",
        description="List the most recent scan/digest/research runs with their status.",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "additionalProperties": False,
        },
        handler=_h_list_recent_runs,
        requires_role="viewer",
    ),
    Tool(
        name="get_company_brief",
        description="Fetch the latest synthesised brief about our own company (uploaded docs + recent signals).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_h_get_company_brief,
        requires_role="viewer",
    ),
    Tool(
        name="get_customer_brief",
        description="Fetch the latest synthesised brief about our customers — what they want, how they're shifting.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_h_get_customer_brief,
        requires_role="viewer",
    ),
    Tool(
        name="get_skill_body",
        description="Read the body of one of the watch's skill prompts. Useful when the user asks what a skill does.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=_h_get_skill_body,
        requires_role="viewer",
    ),
    Tool(
        name="add_competitor",
        description=(
            "Add a new competitor to the watchlist by name. The autofill agent "
            "researches the company (search + fetch) and populates category, "
            "homepage, threat angle, keywords, subreddits, and careers/newsroom "
            "domains automatically — the user can edit any field afterwards at "
            "/admin/competitors/{id}/edit. Takes ~10–30 seconds."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Competitor's brand or company name, e.g. 'Ashby' or 'Greenhouse'.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=_h_add_competitor,
        requires_role="analyst",
        requires_confirmation=True,
        confirmation_summary=lambda i: (
            f"Add {i.get('name', '?')!r} to the watchlist? The autofill agent "
            "will research the company and populate the fields (~10–30s, ~$0.10–0.30)."
        ),
    ),
    Tool(
        name="run_market_digest",
        description="Regenerate the daily market digest from existing findings — LLM only, no new scraping. Takes a few seconds.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_h_run_market_digest,
        requires_role="analyst",
        requires_confirmation=True,
        confirmation_summary=lambda _i: "Regenerate the market digest now? Takes a few seconds, costs roughly $0.10–$0.50.",
    ),
    Tool(
        name="run_deep_research",
        description="Kick off Gemini Deep Research for one competitor. Takes 5–20 minutes; produces a cited dossier.",
        input_schema={
            "type": "object",
            "properties": {
                "competitor": {"type": ["string", "integer"], "description": "Competitor id or name."},
                "agent": {"type": "string", "enum": ["preview", "max"], "description": "preview (faster, cheaper) or max (deeper, costlier). Defaults to preview."},
            },
            "required": ["competitor"],
            "additionalProperties": False,
        },
        handler=_h_run_deep_research,
        requires_role="analyst",
        requires_confirmation=True,
        confirmation_summary=lambda i: (
            f"Run deep research for {i.get('competitor')!r}"
            f" ({i.get('agent', 'preview')} agent)? Takes 5–20 minutes, costs roughly $1–$10."
        ),
    ),
    Tool(
        name="run_market_synthesis",
        description="Kick off a fresh cross-competitor market synthesis (Gemini Deep Research over the whole watchlist). Takes 5–20 minutes.",
        input_schema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["preview", "max"]},
                "window_days": {"type": "integer", "minimum": 1, "maximum": 365},
            },
            "additionalProperties": False,
        },
        handler=_h_run_market_synthesis,
        requires_role="analyst",
        requires_confirmation=True,
        confirmation_summary=lambda i: (
            f"Run a new market synthesis ({i.get('agent', 'preview')} agent, "
            f"last {i.get('window_days', 30)} days)? Takes 5–20 minutes, costs roughly $3–$10."
        ),
    ),
]


# Stage-4 (docs/scenarios/04-scenario-dashboard.md): scenarios chat tools
# live in app/scenarios/chat_tools.py and are appended here so the
# global TOOLS list stays the single registry the agent reads from.
# Imported lazily to avoid a circular at module-load time.
from ..scenarios.chat_tools import SCENARIO_TOOLS  # noqa: E402
TOOLS.extend(SCENARIO_TOOLS)


_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


_ROLE_RANK = {"viewer": 0, "analyst": 1, "admin": 2}


def tools_for_role(role: str) -> list[Tool]:
    rank = _ROLE_RANK.get(role, 0)
    return [t for t in TOOLS if _ROLE_RANK.get(t.requires_role, 0) <= rank]


def get_tool(name: str) -> Tool | None:
    return _BY_NAME.get(name)


def to_anthropic_schema(tools: list[Tool]) -> list[dict]:
    """Render tools in the Anthropic Messages-API tool_use shape."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in tools
    ]


def render_catalog_for_prompt(tools: list[Tool]) -> str:
    """Human-readable bullet list of available tools, written into the
    system prompt so the model has a stable view of what it can do."""
    lines = []
    for t in tools:
        marker = " · requires confirmation" if t.requires_confirmation else ""
        lines.append(f"- `{t.name}` — {t.description}{marker}")
    return "\n".join(lines) if lines else "(no tools available for your role)"


def execute_tool(
    name: str,
    inputs: dict,
    db: Session,
    user: User,
    session: ChatSession | None = None,
) -> tuple[Any, bool]:
    """Run a tool by name with the user's role enforced. Returns
    (output_payload, is_error). Truncation is applied here so every
    handler benefits.

    ``session`` is forwarded to handlers as a keyword. Write handlers
    that kick off background jobs use it to tag the resulting Run with
    ``triggered_by=f"chat:{session.id}"`` so the notifications poller
    can wire completions back to the originating session.
    """
    tool = get_tool(name)
    if tool is None:
        return {"error": f"unknown tool: {name!r}"}, True
    if _ROLE_RANK.get(user.role, 0) < _ROLE_RANK.get(tool.requires_role, 0):
        return {"error": f"tool {name!r} requires role {tool.requires_role}"}, True
    try:
        raw = tool.handler(db, user, _session=session, **(inputs or {}))
    except TypeError as e:
        return {"error": f"bad arguments to {name!r}: {e}"}, True
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}, True
    payload, _truncated = _truncate_output(raw)
    is_error = isinstance(payload, dict) and bool(payload.get("error"))
    return payload, is_error
