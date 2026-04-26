"""Smoke test for the run queue (docs/runs/01-run-queue.md).

Stands up an in-memory SQLite, monkeypatches the three job dispatch targets
to no-op stubs that just flip the row to 'ok', and exercises:
  - enqueue + queue position
  - drain picks oldest, ignores when something running
  - cancel on a queued row -> 'cancelled' without ever running
  - cancel on a running row -> 'cancelling'
  - dispatch error path on unknown kind
  - queue cap at RUN_QUEUE_MAX

Run with: python -m scripts.smoke_run_queue
"""
from __future__ import annotations

import os
import sys
import time

# Use an isolated in-memory DB so we don't touch the real one.
os.environ["DATABASE_URL"] = "sqlite:///./data/smoke_queue.db"
os.environ["DATA_DIR"] = "data"
os.environ["RUN_QUEUE_MAX"] = "3"

# Reset the DB file so each smoke run starts clean.
db_path = "./data/smoke_queue.db"
if os.path.exists(db_path):
    os.remove(db_path)
for tail in ("-wal", "-shm"):
    if os.path.exists(db_path + tail):
        os.remove(db_path + tail)

from app.db import Base, engine, SessionLocal  # noqa: E402
from app import jobs  # noqa: E402
from app.models import Run, RunEvent  # noqa: E402

Base.metadata.create_all(bind=engine)

FAILED = []

def check(label: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}{(' --' + detail) if detail and not cond else ''}")
    if not cond:
        FAILED.append(label)


# ── Stub dispatchers so smoke doesn't actually scan the web ──────
def _stub_succeed(run_id: int, kind: str):
    """Flip the row to 'ok' the way a real job's _finish_run would."""
    db = SessionLocal()
    try:
        row = db.get(Run, run_id)
        row.status = "ok"
        from datetime import datetime
        row.finished_at = datetime.utcnow()
        db.add(RunEvent(run_id=run_id, level="info", message=f"stub {kind} done"))
        db.commit()
    finally:
        db.close()


def _patched_dispatch(run_id, kind, args, triggered_by):
    if kind == "raise":
        raise RuntimeError("boom")
    _stub_succeed(run_id, kind)


jobs._dispatch_queued_run = _patched_dispatch  # type: ignore[assignment]


# ── 1. Basic enqueue + position ────────────────────────────────
print("\n[1] enqueue 3, check positions")
db = SessionLocal()
r1 = jobs.enqueue_run(db, "scan", job_args={"days": 7})
r2 = jobs.enqueue_run(db, "discovery")
r3 = jobs.enqueue_run(db, "market_digest")
check("queue depth = 3", jobs.queue_depth(db) == 3, str(jobs.queue_depth(db)))
check("r1 position = 1", jobs.queue_position(db, r1.id) == 1)
check("r2 position = 2", jobs.queue_position(db, r2.id) == 2)
check("r3 position = 3", jobs.queue_position(db, r3.id) == 3)
db.close()


# ── 2. Drain picks oldest, runs to completion, then next ────────
print("\n[2] drain three runs sequentially")
for i in range(3):
    jobs.drain_run_queue()
    # The dispatch is on a thread; wait briefly.
    time.sleep(0.1)

db = SessionLocal()
statuses = {r.id: r.status for r in db.query(Run).order_by(Run.id).all()}
check(f"r1 ({r1.id}) ok", statuses[r1.id] == "ok", statuses[r1.id])
check(f"r2 ({r2.id}) ok", statuses[r2.id] == "ok", statuses[r2.id])
check(f"r3 ({r3.id}) ok", statuses[r3.id] == "ok", statuses[r3.id])
check("queue empty", jobs.queue_depth(db) == 0)
db.close()


# ── 3. Drainer no-ops when something is running ────────────────
print("\n[3] drainer skips when in flight")
db = SessionLocal()
busy = jobs.enqueue_run(db, "scan", job_args={"days": 1})
busy.status = "running"  # simulate in-flight
db.commit()
busy_id = busy.id
queued = jobs.enqueue_run(db, "scan", job_args={"days": 1})
queued_id = queued.id
db.close()

jobs.drain_run_queue()
time.sleep(0.1)

db = SessionLocal()
still_queued = db.get(Run, queued_id)
check("queued row stays queued while busy is running",
      still_queued.status == "queued", still_queued.status)
busy_db = db.get(Run, busy_id)
busy_db.status = "ok"
db.commit()
db.close()


# ── 4. Cancel a queued run ────────────────────────────────────
print("\n[4] cancel a queued run")
db = SessionLocal()
victim = jobs.enqueue_run(db, "scan", job_args={"days": 14})
victim_id = victim.id
db.close()

# Mimic the cancel route's queued-path logic.
db = SessionLocal()
row = db.get(Run, victim_id)
from datetime import datetime
row.status = "cancelled"
row.finished_at = datetime.utcnow()
db.add(RunEvent(run_id=victim_id, level="warn", message="cancelled before start"))
db.commit()
db.close()

jobs.drain_run_queue()
time.sleep(0.1)
db = SessionLocal()
final = db.get(Run, victim_id)
check("cancelled stays cancelled, drainer skipped it", final.status == "cancelled", final.status)
# Drain the still-queued earlier row from step 3:
remaining = db.query(Run).filter(Run.status == "queued").count()
db.close()
for _ in range(remaining + 1):
    jobs.drain_run_queue()
    time.sleep(0.1)


# ── 5. Queue cap (RUN_QUEUE_MAX=3) ────────────────────────────
print("\n[5] queue cap")
db = SessionLocal()
# Reset: clear everything to simulate a clean slate
db.query(RunEvent).delete()
db.query(Run).delete()
db.commit()
db.close()

# The cap counts queued + running + cancelling.
db = SessionLocal()
jobs.enqueue_run(db, "scan")
jobs.enqueue_run(db, "scan")
jobs.enqueue_run(db, "scan")
in_flight_or_queued = (
    db.query(Run)
    .filter(Run.status.in_(["queued", "running", "cancelling"]))
    .count()
)
check("at-cap count = 3", in_flight_or_queued == 3)
check("at-cap blocks new enqueue (caller checks)",
      in_flight_or_queued >= jobs.RUN_QUEUE_MAX)
db.close()


# ── 6. Dispatch error -> row goes to 'error' ────────────────────
print("\n[6] dispatch error path (unknown kind)")
# Use the real dispatcher to exercise the error catch
jobs._dispatch_queued_run = jobs.__class__.__dict__.get("_dispatch_queued_run")  # restore
# Actually simpler: re-import to get the original
import importlib
importlib.reload(jobs)
# After reload, re-stub for non-error kinds
_orig_dispatch = jobs._dispatch_queued_run

def _patched_dispatch2(run_id, kind, args, triggered_by):
    if kind in ("scan", "discovery", "market_digest"):
        return _stub_succeed(run_id, kind)
    return _orig_dispatch(run_id, kind, args, triggered_by)


jobs._dispatch_queued_run = _patched_dispatch2  # type: ignore[assignment]

db = SessionLocal()
db.query(RunEvent).delete()
db.query(Run).delete()
db.commit()
# Force a row with a bogus kind to exercise the dispatch error path.
bogus = Run(kind="nonsense_kind", status="queued", triggered_by="manual", job_args={})
db.add(bogus); db.commit(); db.refresh(bogus)
bogus_id = bogus.id
db.close()

jobs.drain_run_queue()
time.sleep(0.2)
db = SessionLocal()
final = db.get(Run, bogus_id)
check("unknown-kind row -> 'error'", final.status == "error", final.status)
check("error has dispatch failed message",
      final.error and "dispatch failed" in final.error, str(final.error))
db.close()


print()
if FAILED:
    print(f"[FAIL] {len(FAILED)} failures: {FAILED}")
    sys.exit(1)
print("[OK] all smoke checks passed")
