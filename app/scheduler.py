"""APScheduler wired into the FastAPI lifecycle.
Runs in-process — no external cron needed. Single-replica only for now:
if you scale to >1 web instances, switch to a jobstore with locking.
"""
import os
import json
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import jobs
from .ranker import config as ranker_config

_scheduler: AsyncIOScheduler | None = None


def _load_config_safe() -> dict:
    path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def start():
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    cfg = _load_config_safe()
    scan_hour = int(cfg.get("scan_hour", 8))
    reply_mins = int(cfg.get("reply_check_minutes", 5))
    # Momentum runs an hour before the main scan so its signals are ready for
    # the analyst context. Wraps around midnight if scan_hour is 0.
    momentum_hour = (scan_hour - 1) % 24
    momentum_country = cfg.get("momentum_country", "au")
    signal_retention_days = int(cfg.get("signal_event_retention_days", 180))

    sched = AsyncIOScheduler(timezone=os.environ.get("TZ", "UTC"))

    sched.add_job(
        jobs.run_scan_job,
        CronTrigger(hour=scan_hour, minute=0),
        id="daily_scan",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    sched.add_job(
        jobs.run_reply_check_job,
        IntervalTrigger(minutes=reply_mins),
        id="reply_check",
        replace_existing=True,
    )
    sched.add_job(
        jobs.run_momentum_job,
        CronTrigger(hour=momentum_hour, minute=30),
        id="daily_momentum",
        replace_existing=True,
        misfire_grace_time=3600,
        kwargs={"country": momentum_country},
    )
    # Nightly prune of the ranker signal log (docs/ranker/01-signal-log.md).
    # 02:00 is a quiet hour across timezones; the delete is small (~1 day's
    # worth beyond the retention window per run), so no off-peak gymnastics
    # needed beyond that.
    sched.add_job(
        jobs.prune_signal_events,
        CronTrigger(hour=2, minute=0),
        id="prune_signal_events",
        replace_existing=True,
        misfire_grace_time=3600,
        kwargs={"retention_days": signal_retention_days},
    )
    # Nightly rebuild of ranker preference vectors (spec 02). Runs after
    # the prune so the rollup sees a clean event log. Single-threaded
    # sweep inside the job.
    sched.add_job(
        jobs.nightly_rebuild_preferences_job,
        CronTrigger(hour=2, minute=30),
        id="nightly_rebuild_preferences",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # Monthly positioning-pillar refresh (spec 03). Runs on the 1st of
    # each month at 02:00 — well clear of the daily scan / momentum
    # windows. Configurable via POSITIONING_REFRESH_CRON in the form
    # "minute hour day_of_month". Sleeps 60s between competitors, so a
    # 20-competitor sweep takes ~20 minutes.
    pos_cron = os.environ.get("POSITIONING_REFRESH_CRON", "0 2 1")
    try:
        pmin, phour, pdom = pos_cron.split()
        sched.add_job(
            jobs.run_positioning_refresh_job,
            CronTrigger(minute=pmin, hour=phour, day=pdom),
            id="positioning_refresh",
            replace_existing=True,
            misfire_grace_time=3600,
        )
    except ValueError:
        print(
            f"[scheduler] invalid POSITIONING_REFRESH_CRON={pos_cron!r}; "
            "expected 'min hour dom'. Skipping positioning job."
        )

    sched.start()
    _scheduler = sched
    return sched


def stop():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def get() -> AsyncIOScheduler | None:
    return _scheduler


def next_run_at(job_id: str = "daily_scan"):
    if not _scheduler:
        return None
    job = _scheduler.get_job(job_id)
    return job.next_run_time if job else None


def schedule_incremental_rebuild(user_id: int) -> bool:
    """Queue a one-shot preference rollup for `user_id` if one isn't
    already pending (spec 02 §"Incremental trigger").

    Returns True when a new job was scheduled, False when coalesced.
    Noop when the scheduler isn't running — in dev harnesses or tests
    the caller is expected to rebuild synchronously instead.

    Debounce semantics: the FIRST explicit event schedules a rebuild
    at now + DEBOUNCE_SECONDS. Subsequent events in that window are
    silently dropped, so the rebuild still fires on schedule — the
    goal is to amortize writes for rapid-fire rating clicks without
    pushing the rebuild arbitrarily far into the future.
    """
    if not _scheduler or not _scheduler.running:
        return False
    job_id = f"incremental_rollup_{user_id}"
    if _scheduler.get_job(job_id) is not None:
        return False
    run_at = datetime.utcnow() + timedelta(
        seconds=ranker_config.INCREMENTAL_DEBOUNCE_SECONDS
    )
    _scheduler.add_job(
        jobs.rebuild_user_preferences_job,
        DateTrigger(run_date=run_at),
        id=job_id,
        args=(user_id,),
        misfire_grace_time=ranker_config.INCREMENTAL_DEBOUNCE_SECONDS * 2,
    )
    return True
