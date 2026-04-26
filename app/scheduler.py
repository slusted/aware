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


def _run_market_synthesis_scheduled():
    """Cron entrypoint for the weekly market synthesis. Skips (does not
    queue) when a synthesis is already in flight — manual runs take
    priority, and we never want two overlapping syntheses from the same
    week. Uses the 'max' agent: depth matters more than latency when
    nobody's waiting."""
    from .db import SessionLocal
    db = SessionLocal()
    try:
        load = jobs.current_synthesis_load(db)
    finally:
        db.close()
    if load > 0:
        print(
            "  [scheduler] market_synthesis_weekly skipped — "
            f"{load} synthesis already in flight",
            flush=True,
        )
        return
    jobs.run_market_synthesis_job(
        triggered_by="scheduled", agent="max", window_days=30
    )


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
    # Run-queue drainer (spec docs/runs/01-run-queue.md). Wakes every 2s,
    # picks the oldest queued Run when nothing is in flight, flips it to
    # 'running' and dispatches on a worker thread. max_instances=1 is the
    # critical bit — without it two ticks could race and double-dispatch.
    sched.add_job(
        jobs.drain_run_queue,
        IntervalTrigger(seconds=2),
        id="run_queue_drain",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
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

    # Weekly market synthesis (spec 05). Monday 03:00 local TZ by default —
    # well clear of the daily scan / momentum windows and early enough that
    # the team has a fresh read waiting when Monday morning starts.
    # MARKET_SYNTHESIS_CRON = "min hour day_of_week" (day_of_week in
    # APScheduler's syntax: 0-6 or 'mon'-'sun'). Empty string disables.
    ms_cron = os.environ.get("MARKET_SYNTHESIS_CRON", "0 3 mon")
    if ms_cron.strip():
        try:
            mmin, mhour, mdow = ms_cron.split()
            sched.add_job(
                _run_market_synthesis_scheduled,
                CronTrigger(minute=mmin, hour=mhour, day_of_week=mdow),
                id="market_synthesis_weekly",
                replace_existing=True,
                misfire_grace_time=3600,
            )
        except ValueError:
            print(
                f"[scheduler] invalid MARKET_SYNTHESIS_CRON={ms_cron!r}; "
                "expected 'min hour dow'. Skipping weekly synthesis."
            )

    _register_chat_schedules(sched)
    _register_chat_reply_poll(sched)

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


# ---------- chat scheduled questions (docs/chat/02-scheduled-questions.md) --


def _chat_schedule_job_id(schedule_id: int) -> str:
    return f"chat_schedule_{schedule_id}"


def _run_chat_schedule(schedule_id: int):
    """Cron entrypoint for one ChatSchedule row. Runs the headless
    chat turn end-to-end and fans out the email. All persistence
    happens inside ``run_scheduled_question``; this wrapper exists so
    APScheduler can hold a reference to a top-level function (lambdas
    don't survive a re-register cleanly)."""
    from .chat.scheduled import run_scheduled_question
    try:
        run_scheduled_question(schedule_id)
    except Exception as e:
        # Defensive — the runner already records failures into the
        # schedule row, but a bug above that catch (e.g. import-time
        # error) shouldn't take down the scheduler thread.
        print(f"  [scheduler] chat_schedule {schedule_id} crashed: {e}", flush=True)


def _register_chat_schedules(sched: AsyncIOScheduler) -> None:
    """At boot: load every enabled ChatSchedule and add one cron job
    per row. Call sites: scheduler.start (boot) and the schedules CRUD
    routes (via :func:`register_schedule`) on create/update."""
    from .db import SessionLocal
    from .models import ChatSchedule
    db = SessionLocal()
    try:
        rows = db.query(ChatSchedule).filter(ChatSchedule.enabled.is_(True)).all()
    finally:
        db.close()
    for row in rows:
        try:
            _add_chat_schedule_job(sched, row.id, row.cron)
        except Exception as e:
            print(
                f"  [scheduler] failed to register chat_schedule {row.id} "
                f"with cron={row.cron!r}: {e}",
                flush=True,
            )


def _add_chat_schedule_job(sched: AsyncIOScheduler, schedule_id: int, cron: str):
    """Add or replace one chat-schedule job. ``cron`` is in the
    standard five-field form (``min hour dom month dow``)."""
    trigger = CronTrigger.from_crontab(cron)
    sched.add_job(
        _run_chat_schedule,
        trigger,
        id=_chat_schedule_job_id(schedule_id),
        args=(schedule_id,),
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )


def register_schedule(schedule_id: int) -> bool:
    """Public: re-register one schedule's job after a CRUD edit. Reads
    the row from DB so the call site doesn't have to pass cron text.
    Returns True when a job was added or replaced, False when noop
    (scheduler not running, schedule disabled or missing)."""
    if not _scheduler or not _scheduler.running:
        return False
    from .db import SessionLocal
    from .models import ChatSchedule
    db = SessionLocal()
    try:
        row = db.get(ChatSchedule, schedule_id)
    finally:
        db.close()
    if not row or not row.enabled:
        # Nothing to register; if a stale job exists from a previous
        # enabled state, drop it.
        unregister_schedule(schedule_id)
        return False
    try:
        _add_chat_schedule_job(_scheduler, row.id, row.cron)
        return True
    except Exception as e:
        print(
            f"  [scheduler] register_schedule({schedule_id}) failed: {e}",
            flush=True,
        )
        return False


def unregister_schedule(schedule_id: int) -> bool:
    """Public: drop the cron job for a schedule. Called on disable
    and on delete. Returns True if a job was removed."""
    if not _scheduler or not _scheduler.running:
        return False
    job_id = _chat_schedule_job_id(schedule_id)
    if _scheduler.get_job(job_id) is None:
        return False
    _scheduler.remove_job(job_id)
    return True


# ---------- chat reply poll -------------------------------------------------


def _run_chat_reply_poll():
    from .chat.replies import poll_replies
    try:
        poll_replies()
    except Exception as e:
        print(f"  [scheduler] chat_reply_poll crashed: {e}", flush=True)


def _register_chat_reply_poll(sched: AsyncIOScheduler) -> None:
    """One recurring job that polls IMAP for replies to scheduled-
    question emails. Interval comes from ``CHAT_REPLY_POLL_MINUTES``
    (default 5). Stays registered even when no schedules exist — it's
    a no-op when SMTP/IMAP credentials are missing."""
    interval = int(os.environ.get("CHAT_REPLY_POLL_MINUTES", "5") or "5")
    if interval < 1:
        interval = 1
    sched.add_job(
        _run_chat_reply_poll,
        IntervalTrigger(minutes=interval),
        id="chat_reply_poll",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )


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
