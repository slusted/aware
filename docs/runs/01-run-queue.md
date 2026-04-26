# Spec 01 — Run queue

**Status:** Draft
**Owner:** Simon
**Depends on:** —
**Unblocks:** fire-and-forget multi-trigger UX (kick a discovery + scan back-to-back), cron + manual coexistence without 409s, future per-competitor rerun batching.

## Purpose

Today the trigger endpoints in [app/routes/runs.py:43-48](app/routes/runs.py:43) reject any new run with **409 Conflict** when one is already in flight. Operators have to wait, watch, and click again. This spec adds a **single-slot queue** so any number of trigger clicks succeed: one run executes at a time, the rest wait their turn in the DB.

The keyword here is *single-slot*. Concurrency stays at 1 — same as today — because the scan job already fans out internally with `SCAN_CONCURRENCY` worker threads, and SQLite is the database of record. We're removing the click-and-wait friction, not changing the parallelism model.

## Non-goals

- **Worker pool / parallel runs.** Two scans at once would compete for the same SQLite connection pool, the same Anthropic / Brave / ZenRows budgets, and the same `_StreamToRunEvents` log stream. Keep it 1.
- **External queue infrastructure.** No Celery, RQ, Redis, or SQS. The light-stack constraint stands — we use the DB and the in-process APScheduler that's already wired up in [app/scheduler.py](app/scheduler.py).
- **Persisting queue across restarts of the orchestrator semantics — the queue itself persists**, but a run that was actively `running` when the process died comes back as orphaned (already true today; out of scope to fix here).
- **Market synthesis.** That endpoint uses a different model (`MarketSynthesisReport`), has its own status taxonomy (`queued/running/ready/error`), and a 6h cooldown ([app/routes/runs.py:118-145](app/routes/runs.py:118)). Same pattern would apply but the table and cooldown logic are different — separate spec when we get there.
- **Per-competitor queue / partial scans.** A queued scan still scans every active competitor, same as today.
- **Priority / reordering.** FIFO. If you need to skip the line, cancel the queued run you don't want.

## Design principles

1. **DB is the queue.** A new `queued` status on the existing `runs` table. No new table, no in-memory state that dies on restart.
2. **One drainer.** A single APScheduler interval job picks the next `queued` run and starts it. The scheduler is already in-process and runs once per worker — the existing single-replica assumption already applies ([app/scheduler.py:1-3](app/scheduler.py:1)).
3. **Trigger endpoints stop rejecting.** They insert a `queued` row and return 202 with the run id. Callers don't care whether their work is starting now or in 30s.
4. **The "is anything running" check moves from the endpoints to the drainer.** One source of truth for "can a run start now".
5. **Cancellation works on queued runs too.** Cancelling a `queued` run flips it to `cancelled` without it ever running. Cancelling a `running` run is unchanged from today.
6. **Backwards-compatible URLs and response shapes.** Existing curl / cron / browser callers keep working. The 409 path goes away — the response is now always 202.

## Scope: which run kinds queue?

This spec applies to any run that today checks `Run.status.in_(["running", "cancelling"])` before starting. Concretely:

- `scan` — `POST /api/runs/scan` ([app/routes/runs.py:34](app/routes/runs.py:34))
- `discovery` — `POST /api/runs/discovery` ([app/routes/runs.py:53](app/routes/runs.py:53)) — currently has *no* concurrency check; gains one via the queue.
- `market_digest` — `POST /api/runs/market-digest` ([app/routes/runs.py:59](app/routes/runs.py:59))

Cron-scheduled runs (`daily_scan`, `daily_momentum`, `prune_signal_events`, `nightly_rebuild_preferences`, `positioning_refresh`) are out of scope — they call `jobs.run_*_job` directly, not via the HTTP layer, and have their own misfire grace times. They'll start to *coexist* with the queue (see "Cron interaction") but are not refactored to enqueue.

## Data model change

Single column-domain change to the existing `runs` table.

### Status taxonomy

Today: `pending, running, ok, error, cancelling, cancelled` (informal — the code uses these strings; [app/models.py:103](app/models.py:103)'s docstring is stale).

After: add `queued`. Final set:

| status        | meaning                                                          |
| ------------- | ---------------------------------------------------------------- |
| `queued`      | accepted by the trigger endpoint, waiting for the drainer        |
| `running`     | drainer has started the job; worker is executing                 |
| `cancelling`  | cancel requested while running; worker hasn't bailed yet         |
| `cancelled`   | finished early via cancellation (queued or running)              |
| `ok`          | finished successfully                                            |
| `error`       | finished with an exception                                       |
| `pending`     | legacy, unused — leave alone, don't write new rows with it       |

No migration: SQLite stores `status` as `String(32)` and accepts any string.

### Timestamps

Today, [app/jobs.py:303](app/jobs.py:303) sets `started_at = datetime.utcnow()` at insert time. With queueing, we want to distinguish *when the run was enqueued* from *when work actually began*. Two options:

- **(a) Repurpose `started_at` = enqueue time, add `running_at` = drainer pickup time.** Cleaner semantics but an additive migration.
- **(b) Keep `started_at` as enqueue time, no new column.** Wait time is implicit (= started_at → first RunEvent at level=info from the worker). Simpler.

**Proposal: (b).** A `RunEvent` like `"drainer picked up run #N"` is enough to read wait time off the existing log. Revisit if the dashboard ever wants a "queue depth over time" panel.

## Endpoint behavior

### `POST /api/runs/scan` (and discovery, market_digest)

Before:

```python
existing = db.query(Run).filter(Run.status.in_(["running", "cancelling"])).first()
if existing:
    raise HTTPException(409, ...)
bg.add_task(jobs.run_scan_job, "manual", days)
return {"queued": True, "kind": "scan", "days": days}
```

After:

```python
run = Run(kind="scan", status="queued", triggered_by="manual")
db.add(run)
db.commit()
db.refresh(run)
db.add(RunEvent(run_id=run.id, level="info", message=f"queued (days={days})"))
db.commit()
return {"queued": True, "kind": "scan", "days": days, "run_id": run.id, "queue_position": _queue_position(db, run.id)}
```

- The `BackgroundTasks` call goes away — the drainer picks the run up.
- The response gains `run_id` and `queue_position` (1 = next to run, 2 = one ahead, etc.). Existing callers ignore unknown JSON keys.
- 409 disappears. The endpoint always returns 202. The discovery endpoint, which had no check at all, gains the same enqueue path.
- Trigger args (`days`, etc.) need to survive until the drainer picks the run up. Stash them on the Run row — see "Run args" below.

### Run args

The current trigger functions take Python args (`run_scan_job(triggered_by, freshness_days)`, `run_market_digest_job(triggered_by)`) and pass them via `BackgroundTasks`. Once the queue is the handoff, those args have to live on the Run row.

Add one column:

```python
job_args: Mapped[dict] = mapped_column(JSON, default=dict)
```

The drainer reads `run.kind` + `run.job_args` and dispatches:

```python
DISPATCH = {
    "scan":          lambda r: jobs.run_scan_job(r.triggered_by, r.job_args.get("days")),
    "discovery":     lambda r: jobs.run_discovery_job(),
    "market_digest": lambda r: jobs.run_market_digest_job(r.triggered_by),
}
```

The Run job functions don't change shape — `_start_run` already inserts the row, but with queueing, the row already exists. See "Adapting `_start_run`" below.

### `POST /api/runs/{id}/cancel`

Today only allows `running` and `cancelling` ([app/routes/runs.py:188](app/routes/runs.py:188)). Extend to `queued`:

- `queued` → `cancelled` immediately, write a `RunEvent` "cancelled before start". Return `{cancelled: true, run_id}`.
- `running` → unchanged (sets `cancelling`, the worker bails at next checkpoint).
- Other statuses → 409 as today.

## The drainer

A single APScheduler `IntervalTrigger` job, registered alongside the existing jobs in `start()` ([app/scheduler.py:53](app/scheduler.py:53)).

```python
sched.add_job(
    drain_run_queue,
    IntervalTrigger(seconds=2),
    id="run_queue_drain",
    replace_existing=True,
    max_instances=1,             # critical — APScheduler default is 1, but be explicit
    coalesce=True,
    misfire_grace_time=30,
)
```

`drain_run_queue` (in `app/jobs.py`):

```python
def drain_run_queue():
    db = SessionLocal()
    try:
        # Anything in flight? Then nothing to do.
        in_flight = db.query(Run).filter(Run.status.in_(["running", "cancelling"])).first()
        if in_flight:
            return

        # Next queued run, FIFO by id (same order as started_at == enqueue time).
        nxt = (
            db.query(Run)
            .filter(Run.status == "queued")
            .order_by(Run.id.asc())
            .first()
        )
        if not nxt:
            return

        # Flip to running before dispatch — single-process + max_instances=1 guarantees
        # no other drainer can race us. Commit so the next drainer tick sees the new state.
        nxt.status = "running"
        db.add(RunEvent(run_id=nxt.id, level="info", message="drainer picked up"))
        db.commit()

        kind, args, triggered_by = nxt.kind, dict(nxt.job_args or {}), nxt.triggered_by
        run_id = nxt.id
    finally:
        db.close()

    # Dispatch off-thread so the scheduler tick returns promptly.
    threading.Thread(
        target=_dispatch_queued_run,
        args=(run_id, kind, args, triggered_by),
        daemon=True,
        name=f"run-{run_id}-{kind}",
    ).start()
```

`_dispatch_queued_run` looks up the kind in the DISPATCH map, calls the job function, and traps everything so an unhandled exception flips the row to `error` (the existing `_finish_run` already does this — see "Adapting `_start_run`" for the small refactor).

### Why interval drain instead of "chain on completion"?

A finishing job *could* peek at the queue and start the next one synchronously. But:

- It puts queue logic on every `_finish_run` path, including the error/cancel paths.
- A crashed job that never reaches `_finish_run` strands the queue.
- The scheduler is already running. A 2-second interval costs one cheap query per tick; it's the simplest control loop.

## Adapting `_start_run`

Today [app/jobs.py:301-307](app/jobs.py:301) inserts the Run row. With queueing, the row exists before the job function is called. Refactor:

```python
def _start_run(kind: str, triggered_by: str = "schedule", run_id: int | None = None) -> tuple[Run, Session]:
    db = SessionLocal()
    if run_id is not None:
        run = db.get(Run, run_id)
        # already flipped to "running" by the drainer; just return it
    else:
        run = Run(kind=kind, status="running", triggered_by=triggered_by)
        db.add(run); db.commit(); db.refresh(run)
    return run, db
```

Job functions called via the queue pass `run_id`; cron-scheduled ones (which still call directly, e.g. `daily_scan`) pass nothing and get the legacy "create + run" behavior. No behavior change for cron.

## Cron interaction

Cron-scheduled runs (the daily scan, momentum, etc.) bypass the queue today and create a `running` row directly. They keep doing that — but they now risk *colliding with a queued manual scan*.

Three cases when cron fires:

1. **Nothing running, nothing queued** — cron creates `running` row directly. Same as today.
2. **Something already running (rare; only if a manual run is mid-flight at 08:00)** — cron's `_start_run` would create a *second* `running` row. **This is broken today too** (the trigger endpoints check; cron doesn't). Out of scope to fix in this spec, but flagged.
3. **Something queued, nothing running** — cron creates `running` row, the drainer sees it on its next tick, leaves the queued run alone. The queued run executes after cron finishes. Correct.

If we want full coverage, cron entrypoints should enqueue too — that's a one-line change in each scheduled-job wrapper. Worth doing as a fast-follow, but explicitly *not* required for this spec to ship.

## UI changes

### `/dashboard` — Recent runs table

Today the table renders runs with their status. After: `queued` rows render with a muted dot and the text "queued · position N". Position is computed once per page render: `position = count(queued runs with id <= this.id)`.

### `/runs` — Runs index ([app/templates/runs_index.html](app/templates/runs_index.html))

Today the table is read-only (no actions column). After: add a final **Actions** column. For rows where `status in ("queued", "running")`, render a `Cancel` button:

```html
<button type="button" class="btn btn-ghost btn-sm"
        onclick="cancelRun({{ r.id }})">Cancel</button>
```

`cancelRun(id)` POSTs to `/api/runs/{id}/cancel`, then reloads the page (consistent with how [app/templates/_run_scan_button.html:50](app/templates/_run_scan_button.html:50) handles trigger responses — the runs page is not HTMX-driven, so a reload is the simplest path). For `queued` rows the button is destructive but instant (status flips to `cancelled` synchronously); for `running` rows it's the existing cooperative cancel (button text → "Cancelling…", server flips to `cancelling`, worker bails at next checkpoint).

For all other statuses (`ok`, `error`, `cancelled`) the Actions cell is empty — nothing to cancel.

The dashboard's Recent runs table gets the same Actions column treatment so an operator can cancel a queued run without leaving `/dashboard`.

### Run scan button ([app/templates/_run_scan_button.html](app/templates/_run_scan_button.html))

Today the button is disabled with "Scan running…" text when `is_running`. After:

- The button is **never disabled**. It always says "Run scan" and always succeeds.
- A small badge next to it shows queue depth: "Run scan · 2 waiting" if there are queued runs ahead. (Cheap: same query the dashboard does.)
- Existing freshness-menu, etc., unchanged.

The discovery and market-digest buttons (wherever they live) get the same treatment — drop the "already running" gate, show queue depth.

### Live run panel

The live run panel today tails `RunEvent`s for the current `running` run. Behavior unchanged — when the drainer picks up the next queued run and flips it to `running`, the panel naturally swaps over (it polls `?status=running`). Add one cosmetic touch: when no run is running but `>0` are queued, show "Waiting for queue drain… (next: scan #42)" instead of "No run in flight".

## Failure modes

| Scenario                                              | Behavior                                                                                                                                                                              |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Worker thread crashes mid-job                          | `_finish_run`'s `try/finally` already covers this; row goes to `error`. Drainer picks up the next queued run on its next tick.                                                        |
| Process dies while a run is `running`                  | Same as today — orphaned `running` row sticks around. **Pre-existing bug, out of scope.** Manual fix: UPDATE its status. Future spec could add a startup sweep that flips orphans to `error`. |
| Process dies while a run is `queued`                   | Survives the restart cleanly — drainer picks it up after boot. ✅                                                                                                                      |
| Two API replicas (multi-process)                       | The scheduler comment already calls out "single-replica only" ([app/scheduler.py:3](app/scheduler.py:3)). Two drainers could each pick the same queued row and race. **Same constraint as today**, no new exposure. If we ever scale: add a `SELECT … LIMIT 1` + `UPDATE … WHERE id = ? AND status = 'queued'` with a row-count check in one transaction; the loser sees 0 rows updated and bails. |
| User triggers 50 scans in a tight loop                  | All 50 enqueue. They drain one-at-a-time. Add a soft cap: refuse new triggers if `queued` count ≥ `RUN_QUEUE_MAX` (default 10) with a 429 — protects against runaway clients without re-introducing the 409.                                                                  |
| Drainer interval runs while previous tick still busy    | `max_instances=1` blocks the second tick. The dispatch is on a separate thread, so the *tick itself* finishes in ~10ms; the only way it overlaps is if the DB query stalls — extremely unlikely on local SQLite.                                                              |

## Testing

- Unit: enqueue 3 scans, run drainer manually 3× — first row flips `queued → running`, others stay `queued` until prior finishes. (Use a stub `_dispatch_queued_run` that just writes status=`ok` synchronously.)
- Unit: cancel a `queued` run. Status → `cancelled`. Drainer skips it. Next queued run picks up.
- Unit: cancel a `running` run mid-flight (existing test). Status → `cancelling` → `cancelled`. Drainer picks up next queued.
- Unit: queue cap. With `RUN_QUEUE_MAX=2`, 3rd trigger returns 429. After one drains, a 4th succeeds.
- Unit: `_start_run` with `run_id=` reuses the existing row (no second insert).
- Unit: dispatch error path. Trigger a run kind whose function raises immediately. Row ends `error`, queue continues.
- Smoke: in dev, click Run scan three times in a row. UI shows "Run scan · 2 waiting". Watch them drain one by one. Cancel the second from the runs table — third still runs.

## Acceptance criteria

1. `Run.status` accepts `queued`. `Run` has a `job_args` JSON column. No data migration needed (SQLite tolerates the new column with `default=dict` for existing rows).
2. `POST /api/runs/scan|discovery|market-digest` always return 202 (or 429 only when queue cap hit). They never return 409. Response includes `run_id` and `queue_position`.
3. `POST /api/runs/{id}/cancel` accepts `queued` runs and short-circuits them to `cancelled`.
4. `app/jobs.drain_run_queue` exists and is registered as a 2-second `IntervalTrigger` job in `app.scheduler.start()` with `max_instances=1`.
5. `_start_run` accepts an optional `run_id` and reuses the existing row when provided.
6. The Run scan button in [app/templates/_run_scan_button.html](app/templates/_run_scan_button.html) is never disabled. It shows queue depth when `>0`.
7. The Recent runs table on `/dashboard` and the `/runs` index render `queued` rows with a position indicator and a `Cancel` button. Clicking Cancel on a queued row flips it to `cancelled` immediately and the row updates on reload. Clicking Cancel on a running row triggers the existing cooperative cancel.
8. With three back-to-back manual scan triggers and `SCAN_CONCURRENCY=4`, all three runs eventually finish in FIFO order. No 409s. No `Run` rows stuck `queued` after the last one finishes.
9. Tests in `tests/test_run_queue.py` cover enqueue, drain, cancel-queued, cancel-running, queue cap, and dispatch-error-continues. All green.

## Open questions

1. **Drainer interval.** 2s feels fine. Faster (500ms) makes the queue feel snappier; slower (5s) is gentler on SQLite. Proposal: 2s and revisit if the UX feels laggy.
2. **Default queue cap.** 10 is plausible. The real backstop is humans noticing the queue is full. Configurable via `RUN_QUEUE_MAX`.
3. **Should cron entrypoints enqueue too?** This spec says no (keep the change scoped). But the "two `running` rows" failure mode in case 2 above is a pre-existing bug that this would fix. Worth a short fast-follow spec — call it spec 02.
4. **Cancel cascade?** If a `queued` market-digest is cancelled because the analyst lost interest, do we also cancel any `queued` follow-on runs they fired? Probably not — they enqueued each one explicitly. No cascade.
5. **Drainer vs. autocompact.** The Anthropic context auto-compaction is irrelevant here, but the *human* reading the runs page may scroll through dozens of `queued`/`cancelled` rows once queueing is normal. Not blocking; consider hiding `cancelled` rows older than 24h on the dashboard list (separate ticket).

## What this unblocks

- Spec 02 (proposed): cron entrypoints enqueue rather than running directly. Closes the "two `running` rows" hole.
- A future "scan all competitors that haven't been scanned in N days" batch operation that fires N enqueue calls and lets the queue handle pacing — no new infra.
- Operator UX: kicking off a discovery + scan back-to-back without babysitting the page. This is the 80% of why this spec exists.
