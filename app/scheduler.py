"""APScheduler wired into the FastAPI lifecycle.
Runs in-process — no external cron needed. Single-replica only for now:
if you scale to >1 web instances, switch to a jobstore with locking.
"""
import os
import json
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import jobs

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
