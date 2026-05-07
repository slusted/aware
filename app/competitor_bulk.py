"""Bulk-add background worker for the watchlist.

Used by the /admin/competitors/bulk-new page so the user can paste a list
of names, kick off one job, and watch them stream in as the autofill
agent fills each row. Same write path as the New Competitor form — just
looped.

Job state is held in-process. If the server restarts mid-batch the
in-flight job is lost; rows already committed stay in the DB. That's
acceptable for an admin-only one-off operation.
"""

import threading
import uuid
from datetime import datetime
from typing import Iterable

from .competitor_autofill import autofill
from .config_sync import sync_db_to_config
from .db import SessionLocal
from .models import Competitor
from . import logos as logos_cache


_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}


def parse_names(raw: str) -> list[str]:
    """Split a paste-blob into a deduped, ordered list of names. Accepts
    one-per-line or comma-separated. Preserves first-seen order."""
    if not raw:
        return []
    pieces: list[str] = []
    for line in raw.replace("\r", "\n").split("\n"):
        for chunk in line.split(","):
            n = chunk.strip()
            if n:
                pieces.append(n)
    seen: set[str] = set()
    out: list[str] = []
    for n in pieces:
        key = n.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def start_bulk_add(names: Iterable[str], company: str, industry: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    items = [
        {"name": n, "status": "queued", "error": None, "competitor_id": None}
        for n in names
    ]
    job = {
        "id": job_id,
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "company": company,
        "industry": industry,
        "items": items,
        "done": False,
    }
    with _LOCK:
        _JOBS[job_id] = job
    threading.Thread(
        target=_run_job, args=(job_id,),
        name=f"bulk-add-{job_id}", daemon=True,
    ).start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        return {
            **job,
            "items": [dict(it) for it in job["items"]],
        }


def list_recent_jobs(limit: int = 10) -> list[dict]:
    """Most-recent-first snapshot of the in-memory job table. Used by the
    bulk-add page to surface in-flight or just-finished batches when the
    operator lands without a `?job=` query param. Wiped on server restart
    — that's accepted, same as get_job."""
    with _LOCK:
        jobs = list(_JOBS.values())
    jobs.sort(key=lambda j: j["started_at"], reverse=True)
    out: list[dict] = []
    for j in jobs[:limit]:
        items = j["items"]
        finished = sum(
            1 for it in items
            if it["status"] in ("added", "reactivated", "skipped", "error")
        )
        out.append({
            "id": j["id"],
            "started_at": j["started_at"],
            "done": j["done"],
            "total": len(items),
            "finished": finished,
            "added": sum(1 for it in items if it["status"] == "added"),
            "reactivated": sum(1 for it in items if it["status"] == "reactivated"),
            "skipped": sum(1 for it in items if it["status"] == "skipped"),
            "errored": sum(1 for it in items if it["status"] == "error"),
        })
    return out


def _set_item(job_id: str, idx: int, **fields) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["items"][idx].update(fields)


def _mark_done(job_id: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["done"] = True
        job["finished_at"] = datetime.utcnow().isoformat()


def _run_job(job_id: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        company = job["company"]
        industry = job["industry"]
        items = list(job["items"])

    db = SessionLocal()
    domains_to_logo: list[str] = []
    try:
        for idx, item in enumerate(items):
            name = item["name"]
            _set_item(job_id, idx, status="working")

            existing = (
                db.query(Competitor)
                .filter(Competitor.name.ilike(name))
                .first()
            )
            if existing and existing.active:
                _set_item(
                    job_id, idx,
                    status="skipped",
                    error=f"already on watchlist (id={existing.id})",
                    competitor_id=existing.id,
                )
                continue

            try:
                result = autofill(name, company, industry)
            except Exception as e:
                _set_item(job_id, idx, status="error",
                          error=f"autofill: {type(e).__name__}: {e}")
                continue

            data = result.get("data") or {}

            if existing:
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
                    for v in data.get(field) or []:
                        if v and v not in current:
                            current.append(v)
                    setattr(existing, field, current)
                try:
                    db.commit()
                except Exception as e:
                    db.rollback()
                    _set_item(job_id, idx, status="error",
                              error=f"db: {type(e).__name__}: {e}")
                    continue
                _set_item(job_id, idx, status="reactivated",
                          competitor_id=existing.id)
                if existing.homepage_domain:
                    domains_to_logo.append(existing.homepage_domain)
                continue

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
            try:
                db.add(comp)
                db.commit()
                db.refresh(comp)
            except Exception as e:
                db.rollback()
                _set_item(job_id, idx, status="error",
                          error=f"db: {type(e).__name__}: {e}")
                continue
            _set_item(job_id, idx, status="added", competitor_id=comp.id)
            if comp.homepage_domain:
                domains_to_logo.append(comp.homepage_domain)

        try:
            sync_db_to_config(db)
        except Exception:
            pass
    finally:
        db.close()
        _mark_done(job_id)

    for d in domains_to_logo:
        try:
            logos_cache.fetch_and_store(d)
        except Exception:
            pass
