"""One-shot 90-day historical scan kicked off when a competitor is added.

When a new competitor lands on the watchlist the daily cron only starts
populating its findings tomorrow. To make the profile page useful from
day one we run a single scan with a 90-day freshness window — every
news/Reddit/newsroom finding gets stored with its real published_at, so
the per-day timeline looks populated immediately.

Mechanics:
- Tracked run (parallel-safe with the daily scan; concurrency=None so a
  20-row bulk import doesn't trip a cap).
- Reuses jobs._scan_and_review_one — same scan path, same hash dedup,
  same review synth as the daily job, just with TAVILY_DAYS bumped.
- TAVILY_DAYS is restored on exit. The race window with a concurrent
  daily scan is small (cron fires once a day; backfill is on-demand at
  add time) and a wider window only means *more* findings, not wrong
  ones — dedup is by Finding.hash.
- Findings persisted here are dedup'd by tomorrow's daily scan via the
  Finding.hash check in _scan_and_review_one (line ~811 of jobs.py),
  so no double-ingest. memory.json's seen_hashes is also synced via
  save_memory() at the end so the scanner can early-skip URL fetches.

Triggered from:
- POST /admin/competitors (single-add)
- bulk_competitor_add tracked run (per added/reactivated row)
- chat tool add_competitor

Reactivations don't trigger a backfill: those competitors already have
historical Findings.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .db import SessionLocal
from .jobs import (
    RUN_KINDS,
    RunCancelled,
    RunKindSpec,
    _finish_run,
    _log,
    _scan_and_review_one,
    _start_run,
    check_cancel,
    clear_cancel,
)
from .models import Competitor, Run, RunEvent


BACKFILL_DAYS = int(os.environ.get("COMPETITOR_BACKFILL_DAYS", "90"))

# Bound concurrency in-process. SQLite is single-writer (even under WAL),
# so stacking many backfills stalls user request handlers behind the
# writer queue — the website goes unresponsive while a 30-competitor
# bulk import / "backfill missing" sweep runs flat-out. A small pool
# keeps the website responsive while still parallelising the slow part
# (Tavily + Anthropic round-trips). Tune via env if you want more.
_MAX_CONCURRENT = max(1, int(os.environ.get("COMPETITOR_BACKFILL_CONCURRENCY", "2")))
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """Lazy-initialise so import-time cost is zero on workers that never
    hit a backfill (e.g. one-off scripts)."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=_MAX_CONCURRENT,
                    thread_name_prefix="competitor-backfill",
                )
    return _executor


def _competitor_dict_from_config(name: str) -> dict | None:
    """Pull the canonical comp_dict from config.json — the same shape the
    daily scan iterates. sync_db_to_config() runs in every add path
    before this kicks off, so the row should be present."""
    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None
    for entry in cfg.get("competitors", []) or []:
        if (entry.get("name") or "").lower() == name.lower():
            return entry
    return None


def _load_company_industry() -> tuple[str, str]:
    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return (
        cfg.get("company", "our company"),
        cfg.get("industry", "our industry"),
    )


def _load_topics() -> list[str]:
    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return []
    return list(cfg.get("watch_topics") or [])


def _run_competitor_backfill_target(
    *,
    run_id: int,
    competitor_id: int,
    days: int | None = None,
) -> None:
    """Tracked-run target. One competitor, one wide-window scan, one
    optional review synth. Failures are isolated to this run row."""
    run, db = _start_run("competitor_backfill", run_id=run_id)
    status: str = "ok"
    error: str | None = None

    try:
        c = db.get(Competitor, competitor_id)
        if c is None:
            _log(db, run, f"competitor id={competitor_id} not found", level="error")
            status = "error"
            error = f"competitor id={competitor_id} not found"
            return

        check_cancel(run.id)

        comp_dict = _competitor_dict_from_config(c.name)
        if comp_dict is None:
            # Fallback for the rare case where sync_db_to_config hasn't
            # landed yet. Mirror config_sync._to_json so the scanner sees
            # the same shape it would on a normal scan.
            comp_dict = {
                "name": c.name,
                "keywords": list(c.keywords or []),
                "subreddits": list(c.subreddits or []),
                "careers_domains": list(c.careers_domains or []),
                "newsroom_domains": list(c.newsroom_domains or []),
                "ats_tenants": list(c.ats_tenants or []),
            }
            if c.homepage_domain:
                comp_dict["homepage_domain"] = c.homepage_domain
            if c.category:
                comp_dict["category"] = c.category
            if c.min_relevance_score is not None:
                comp_dict["min_relevance_score"] = float(c.min_relevance_score)
            if c.social_score_multiplier is not None:
                comp_dict["social_score_multiplier"] = float(c.social_score_multiplier)

        topics = _load_topics()
        company, industry = _load_company_industry()

        window = int(days if days is not None else BACKFILL_DAYS)
        if window <= 0:
            _log(db, run, "BACKFILL_DAYS<=0 — backfill disabled, no-op")
            return

        _log(
            db,
            run,
            f"backfill {c.name}: last {window} days, freshness via TAVILY_DAYS",
        )

        # The scanner module uses a module-level TAVILY_DAYS as its default
        # freshness across every search_tavily() call. We swap it for the
        # duration of this backfill and restore on exit. See docstring at
        # top of this file for the race-window discussion.
        from app.search_providers import tavily as _tavily
        from scanner import load_memory, save_memory

        prev_days = _tavily.TAVILY_DAYS
        _tavily.TAVILY_DAYS = window
        try:
            memory = load_memory()
            with contextlib.closing(_StreamSilencer()):
                result = _scan_and_review_one(
                    comp_dict, topics, memory, company, industry, run.id,
                )
            # Persist seen_hashes so tomorrow's daily scan can early-skip
            # the URLs we just ingested instead of re-fetching them.
            try:
                save_memory(memory)
            except Exception as e:
                _log(db, run, f"save_memory warning: {e}", level="warning")
        finally:
            _tavily.TAVILY_DAYS = prev_days

        if result.get("cancelled"):
            status = "cancelled"
        elif result.get("error"):
            status = "error"
            error = result["error"]
        else:
            n = len(result.get("findings") or [])
            _log(db, run, f"backfill done: {n} findings considered for {c.name}")
    except RunCancelled:
        status = "cancelled"
    except Exception as e:
        status = "error"
        error = f"{type(e).__name__}: {e}"
        db.add(RunEvent(run_id=run.id, level="error", message=error))
        db.commit()
    finally:
        _finish_run(db, run, status=status, error=error)
        clear_cancel(run.id)


class _StreamSilencer:
    """No-op context to keep the with-block parallel to run_scan_job's
    redirect_stdout pattern. We don't tee scanner stdout into RunEvents
    here because the daily scan's _StreamToRunEvents class wires in the
    current_run_id for that. A backfill's per-line log is best-effort
    via _log() above."""

    def close(self) -> None:
        pass


# Self-register on import. Concurrency is enforced by the in-process
# ThreadPoolExecutor below (kick_off submits there), not by
# start_tracked_run — kick_off bypasses that helper so overflow gets
# queued in the executor instead of raising ConcurrencyCapExceeded and
# silently dropping backfills (which would regress bulk_competitor_add
# and the admin "backfill missing" button).
RUN_KINDS["competitor_backfill"] = RunKindSpec(
    name="competitor_backfill",
    mode="tracked",
    target=_run_competitor_backfill_target,
    concurrency=None,
    detail_url=lambda r: (
        f"/competitors/{(r.job_args or {}).get('competitor_id')}"
        if (r.job_args or {}).get("competitor_id") is not None
        else "/runs"
    ),
)


def _executor_worker(run_id: int, competitor_id: int, days: int) -> None:
    """Run inside one of the bounded executor threads. Calls the registered
    target inline so concurrency is pinned at _MAX_CONCURRENT — submissions
    beyond the cap sit in the executor's internal queue instead of spawning
    extra threads or being dropped."""
    try:
        _run_competitor_backfill_target(
            run_id=run_id, competitor_id=competitor_id, days=days,
        )
    except Exception as e:
        # The target's own try/finally normally flips the row to 'error'.
        # This is belt-and-braces for crashes that happen before _start_run
        # (import errors, missing job_args, etc.) so the row doesn't sit
        # 'running' forever.
        tb = traceback.format_exc()
        d = SessionLocal()
        try:
            row = d.get(Run, run_id)
            if row and row.status in ("running", "cancelling"):
                row.status = "error"
                row.error = f"{type(e).__name__}: {e}"
                row.finished_at = datetime.utcnow()
                d.add(RunEvent(
                    run_id=run_id, level="error",
                    message=f"backfill executor crashed: {e}\n{tb}",
                ))
                d.commit()
        finally:
            d.close()


def kick_off(db, competitor_id: int, *, triggered_by: str = "manual") -> None:
    """Helper for trigger callsites: queue a backfill run on the bounded
    in-process executor. Returns immediately. Catches any kickoff error
    so the add-competitor path never fails because the backfill couldn't
    start; failures land on the Run row, recoverable from /runs."""
    if BACKFILL_DAYS <= 0:
        return
    try:
        # Create the Run row eagerly so it appears on /runs the moment the
        # operator clicks. Status='running' matches the existing
        # start_tracked_run shape — the row is "claimed", just waiting for
        # an executor slot. The cap is short (2 by default) so the wait is
        # bounded.
        run = Run(
            kind="competitor_backfill",
            status="running",
            triggered_by=triggered_by,
            job_args={"competitor_id": competitor_id, "days": BACKFILL_DAYS},
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        db.add(RunEvent(
            run_id=run.id, level="info",
            message=f"queued behind backfill cap={_MAX_CONCURRENT}; "
                    f"will start once a slot frees up",
        ))
        db.commit()
        run_id = run.id
    except Exception as e:
        print(f"[competitor_backfill] kickoff row-create failed for id={competitor_id}: {e}")
        return

    try:
        _get_executor().submit(
            _executor_worker, run_id, competitor_id, BACKFILL_DAYS,
        )
    except Exception as e:
        # Submission itself shouldn't fail under normal load, but if it
        # does, mark the row error so it doesn't sit 'running' forever.
        print(f"[competitor_backfill] executor submit failed for run {run_id}: {e}")
        try:
            row = db.get(Run, run_id)
            if row and row.status in ("running", "queued"):
                row.status = "error"
                row.error = f"executor submit failed: {e}"
                row.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            db.rollback()
