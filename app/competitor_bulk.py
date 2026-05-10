"""Bulk-add background worker for the watchlist.

Used by the /admin/competitors/bulk-new page so the user can paste a list
of names, kick off one job, and watch them stream in as the autofill
agent fills each row.

Implemented as a tracked run (see app/jobs.py:start_tracked_run): each
pasted name becomes a RunEvent with structured `meta` containing
{item_idx, name, phase, competitor_id, error}. The bulk-new page reduces
those events back into a per-row table via items_for_run() below.

Restart safety is the same as any other run: a 'running' row at the
moment the process dies becomes orphaned (pre-existing limitation of the
run lifecycle, see docs/runs/01-run-queue.md). Rows already committed
stay in the DB.
"""

from typing import Any

from . import competitor_backfill
from .competitor_autofill import autofill
from .config_sync import sync_db_to_config
from .jobs import (
    RUN_KINDS,
    RunCancelled,
    RunKindSpec,
    _finish_run,
    _start_run,
    check_cancel,
    clear_cancel,
)
from .models import Competitor, Run, RunEvent
from . import logos as logos_cache


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


# Phases the UI knows how to render. 'queued' is emitted up-front for every
# item so the table has all rows immediately; the rest replace it as the
# worker progresses.
_PHASE_TERMINAL = {"added", "reactivated", "skipped", "error"}


def _emit(
    db,
    run_id: int,
    idx: int,
    name: str,
    phase: str,
    *,
    competitor_id: int | None = None,
    error: str | None = None,
) -> None:
    """Per-item progress event. Latest event for a given item_idx wins
    when items_for_run() reduces the stream into a row table."""
    db.add(
        RunEvent(
            run_id=run_id,
            level="error" if phase == "error" else "info",
            message=f"[{idx + 1}] {name}: {phase}"
            + (f" — {error}" if error else ""),
            meta={
                "item_idx": idx,
                "name": name,
                "phase": phase,
                "competitor_id": competitor_id,
                "error": error,
            },
        )
    )
    db.commit()


def _run_bulk_competitor_add_target(
    *,
    run_id: int,
    names: list[str],
    company: str,
    industry: str,
) -> None:
    """Tracked-run target. Signature matches the RunKindSpec contract:
    (*, run_id, **job_args) -> None. Args come from the Run row's
    job_args JSON column, populated at trigger time."""
    run, db = _start_run("bulk_competitor_add", run_id=run_id)
    status: str = "ok"
    error: str | None = None
    domains_to_logo: list[str] = []

    # Seed every row as 'queued' so the table renders immediately.
    for idx, name in enumerate(names):
        _emit(db, run_id, idx, name, "queued")

    try:
        for idx, name in enumerate(names):
            check_cancel(run_id)
            _emit(db, run_id, idx, name, "working")

            existing = (
                db.query(Competitor)
                .filter(Competitor.name.ilike(name))
                .first()
            )
            if existing and existing.active:
                _emit(
                    db, run_id, idx, name, "skipped",
                    competitor_id=existing.id,
                    error=f"already on watchlist (id={existing.id})",
                )
                continue

            try:
                result = autofill(name, company, industry)
            except Exception as e:
                _emit(
                    db, run_id, idx, name, "error",
                    error=f"autofill: {type(e).__name__}: {e}",
                )
                continue

            data: dict[str, Any] = result.get("data") or {}

            if existing:
                # Reactivate: union list-fields, fill blank scalar fields.
                existing.active = True
                for f in (
                    "category", "threat_angle", "homepage_domain",
                    "app_store_id", "play_package", "trends_keyword",
                ):
                    v = data.get(f)
                    if v and not getattr(existing, f, None):
                        setattr(existing, f, v)
                for f in (
                    "keywords", "subreddits",
                    "careers_domains", "newsroom_domains",
                ):
                    current = list(getattr(existing, f, None) or [])
                    for v in data.get(f) or []:
                        if v and v not in current:
                            current.append(v)
                    setattr(existing, f, current)
                try:
                    db.commit()
                except Exception as e:
                    db.rollback()
                    _emit(
                        db, run_id, idx, name, "error",
                        error=f"db: {type(e).__name__}: {e}",
                    )
                    continue
                _emit(
                    db, run_id, idx, name, "reactivated",
                    competitor_id=existing.id,
                )
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
                _emit(
                    db, run_id, idx, name, "error",
                    error=f"db: {type(e).__name__}: {e}",
                )
                continue
            _emit(db, run_id, idx, name, "added", competitor_id=comp.id)
            if comp.homepage_domain:
                domains_to_logo.append(comp.homepage_domain)
            # Kick the 90-day historical backfill for this fresh row. We do
            # it here (not after sync_db_to_config below) because the
            # backfill target re-reads config.json — but the helper falls
            # back to building a comp_dict directly off the row if the
            # sync hasn't landed yet, so order is forgiving either way.
            competitor_backfill.kick_off(
                db, comp.id, triggered_by="bulk_competitor_add",
            )

        try:
            sync_db_to_config(db)
        except Exception:
            pass
    except RunCancelled:
        status = "cancelled"
    except Exception as e:
        status = "error"
        error = f"{type(e).__name__}: {e}"
        db.add(RunEvent(run_id=run_id, level="error", message=error))
        db.commit()
    finally:
        _finish_run(db, run, status=status, error=error)
        clear_cancel(run_id)

    # Best-effort logo prefetch outside the run lifecycle. Failures here
    # don't affect the run's outcome — logos backfill on demand anyway.
    for d in domains_to_logo:
        try:
            logos_cache.fetch_and_store(d)
        except Exception:
            pass


def items_for_run(db, run_id: int) -> list[dict]:
    """Reduce the per-item RunEvents for a bulk run into one row per
    item, latest phase wins. Returned in submitted (item_idx) order.
    Used by the bulk-new GET handler to render the progress table."""
    events = (
        db.query(RunEvent)
        .filter(RunEvent.run_id == run_id)
        .order_by(RunEvent.id.asc())
        .all()
    )
    by_idx: dict[int, dict] = {}
    for ev in events:
        meta = ev.meta or {}
        idx = meta.get("item_idx")
        if idx is None:
            continue
        by_idx[idx] = {
            "name": meta.get("name"),
            "status": meta.get("phase"),
            "competitor_id": meta.get("competitor_id"),
            "error": meta.get("error"),
        }
    return [by_idx[k] for k in sorted(by_idx.keys())]


def list_recent_jobs(db, limit: int = 10) -> list[dict]:
    """Most-recent-first list of bulk-add Runs with summary counts. Used by
    the bulk-add page to surface in-flight or just-finished batches when the
    operator lands without a `?run_id=` query param. Replaces the original
    in-memory snapshot from PR #120 — same UI shape, now durable across
    process restarts."""
    runs = (
        db.query(Run)
        .filter(Run.kind == "bulk_competitor_add")
        .order_by(Run.started_at.desc())
        .limit(limit)
        .all()
    )
    out: list[dict] = []
    for r in runs:
        items = items_for_run(db, r.id)
        out.append({
            "id": r.id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "done": r.status not in ("running", "cancelling"),
            "total": len(items),
            "finished": sum(
                1 for it in items
                if it["status"] in ("added", "reactivated", "skipped", "error")
            ),
            "added": sum(1 for it in items if it["status"] == "added"),
            "reactivated": sum(1 for it in items if it["status"] == "reactivated"),
            "skipped": sum(1 for it in items if it["status"] == "skipped"),
            "errored": sum(1 for it in items if it["status"] == "error"),
        })
    return out


# Self-register the kind on import. Routes import this module before
# calling start_tracked_run, so the registration is always in place by
# the time the helper looks the kind up.
RUN_KINDS["bulk_competitor_add"] = RunKindSpec(
    name="bulk_competitor_add",
    mode="tracked",
    target=_run_bulk_competitor_add_target,
    concurrency=1,
    detail_url=lambda r: f"/admin/competitors/bulk-new?run_id={r.id}",
)
