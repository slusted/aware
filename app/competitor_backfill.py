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
from .models import Competitor, RunEvent


BACKFILL_DAYS = int(os.environ.get("COMPETITOR_BACKFILL_DAYS", "90"))


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


# Self-register on import so the trigger callsites can hand the kind
# string to start_tracked_run. concurrency=None: a 20-row bulk add fires
# 20 backfills in parallel; rate-limit pressure is on Tavily/Anthropic,
# not on us.
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


def kick_off(db, competitor_id: int, *, triggered_by: str = "manual") -> None:
    """Helper for trigger callsites: spawn a backfill run and swallow any
    error so the add-competitor path never fails because the backfill
    couldn't start. The original Run row already exists for visibility."""
    if BACKFILL_DAYS <= 0:
        return
    try:
        from .jobs import start_tracked_run
        start_tracked_run(
            db,
            "competitor_backfill",
            triggered_by=triggered_by,
            job_args={"competitor_id": competitor_id, "days": BACKFILL_DAYS},
        )
    except Exception as e:
        # Never block competitor creation on a backfill kickoff failure.
        # The /runs page is the recovery path — operator can re-trigger.
        print(f"[competitor_backfill] kickoff failed for id={competitor_id}: {e}")
