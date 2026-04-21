"""End-to-end verification for the ranker signal log (docs/ranker/01-signal-log.md).

Runs every acceptance criterion for spec 01 against a throwaway SQLite DB
so the suite is hermetic and can run in CI without touching dev data.

Usage:
    python scripts/verify_signal_log.py

Exit code:
    0 = all checks passed
    1 = one or more checks failed (details printed above)

Why this instead of pytest: the codebase has no existing test infra and
pytest isn't a declared dependency. This script matches the scripts/
convention already in use (backfill_*.py etc.) and can be wrapped by a
pytest fixture later if the team introduces one.
"""
from __future__ import annotations

import os
import secrets
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Force a fresh temp DB for every invocation. MUST be set before any app
# module imports so `app.db.DATABASE_URL` reads the right value.
_tmp = Path(tempfile.mkdtemp(prefix="signal_log_verify_")) / "verify.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

# Now safe to import app modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import create_engine, event, inspect, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base  # noqa: E402
from app import models  # noqa: E402
from app.auth import SESSION_COOKIE  # noqa: E402
from app.main import app  # noqa: E402
from app.jobs import prune_signal_events  # noqa: E402


# ────────────────────────── Test harness ─────────────────────────────

_failures: list[str] = []
_passes: int = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record a single acceptance check. Printed as PASS/FAIL with detail."""
    global _passes
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f"  — {detail}"
    print(line)
    if ok:
        _passes += 1
    else:
        _failures.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ────────────────────────── Fixtures ─────────────────────────────────

engine = create_engine(os.environ["DATABASE_URL"])
Base.metadata.create_all(bind=engine)

with Session(engine) as s:
    u = models.User(email="t@t", name="t", role="admin", is_active=True)
    s.add(u)
    s.commit()
    s.refresh(u)
    USER_ID = u.id

    FINDING_IDS: list[int] = []
    for i in range(5):
        f = models.Finding(
            competitor="X", source="test", hash=f"h{i}",
            title=f"Finding {i}", created_at=datetime.utcnow() - timedelta(hours=i),
        )
        s.add(f)
        s.flush()
        FINDING_IDS.append(f.id)

    token = secrets.token_hex(16)
    s.add(models.AuthSession(
        token=token, user_id=USER_ID,
        expires_at=datetime.utcnow() + timedelta(days=1),
    ))
    s.commit()

client = TestClient(app)
client.cookies.set(SESSION_COOKIE, token)


def count_events(**filters) -> int:
    with Session(engine) as s:
        q = s.query(models.UserSignalEvent)
        for k, v in filters.items():
            q = q.filter(getattr(models.UserSignalEvent, k) == v)
        return q.count()


# ────────────────────────── §1: API validation ───────────────────────
section("Events API: /api/signals/event")

r = client.post(
    "/api/signals/event",
    json={"event_type": "view", "source": "stream", "finding_id": FINDING_IDS[0]},
)
check(
    "valid view accepted (204)",
    r.status_code == 204 and count_events(event_type="view") == 1,
)

r = client.post(
    "/api/signals/event",
    json={"event_type": "view", "source": "stream", "finding_id": FINDING_IDS[0]},
)
check(
    "duplicate view within 5m silently dropped",
    r.status_code == 204 and count_events(event_type="view") == 1,
    "second POST returned 204 but no new row",
)

r = client.post(
    "/api/signals/event",
    json={"event_type": "bogus", "source": "stream", "finding_id": FINDING_IDS[0]},
)
check("unknown event_type → 400", r.status_code == 400)

r = client.post(
    "/api/signals/event",
    json={"event_type": "pin", "source": "stream", "finding_id": FINDING_IDS[0]},
)
check("server-only type rejected from client → 400", r.status_code == 400)

r = client.post(
    "/api/signals/event",
    json={"event_type": "rate_up", "source": "stream"},
)
check("rating without finding_id → 400", r.status_code == 400)

r = client.post(
    "/api/signals/event",
    json={"event_type": "rate_up", "source": "stream", "finding_id": 99999},
)
check("unknown finding_id → 404", r.status_code == 404)

# Batch
r = client.post(
    "/api/signals/events/batch",
    json={"events": [
        {"event_type": "view", "source": "stream", "finding_id": FINDING_IDS[0]},  # duplicate of earlier
        {"event_type": "rate_up", "source": "stream", "finding_id": FINDING_IDS[1]},
        {"event_type": "open", "source": "stream", "finding_id": FINDING_IDS[2]},
    ]},
)
check(
    "batch with dup + new events inserts only the new ones",
    r.status_code == 204
    and count_events(event_type="view") == 1  # still 1, dup dropped
    and count_events(event_type="rate_up") == 1
    and count_events(event_type="open") == 1,
)

r = client.post(
    "/api/signals/events/batch",
    json={"events": [{"event_type": "view", "source": "stream", "finding_id": FINDING_IDS[0]}] * 101},
)
check("oversize batch (101) → 400", r.status_code == 400)

r = client.post("/api/signals/events/batch", json={"events": []})
check("empty batch → 204", r.status_code == 204)


# ────────────────────────── §2: Dual-write transitions ────────────────
section("Dual-write on SignalView state transitions")

# Use a clean finding so we have an isolated transition history.
TRANS_FID = FINDING_IDS[3]


def events_for_finding(fid: int) -> list[str]:
    with Session(engine) as s:
        return [
            e.event_type
            for e in s.query(models.UserSignalEvent)
            .filter(models.UserSignalEvent.finding_id == fid)
            .order_by(models.UserSignalEvent.id)
            .all()
        ]


before = events_for_finding(TRANS_FID)

client.post(f"/api/findings/{TRANS_FID}/view", json={"state": "seen"})
check(
    "seen transition emits nothing",
    events_for_finding(TRANS_FID) == before,
)

client.post(f"/api/findings/{TRANS_FID}/view", json={"state": "pinned"})
check(
    "seen → pinned emits pin",
    events_for_finding(TRANS_FID) == [*before, "pin"],
)

client.post(f"/api/findings/{TRANS_FID}/view", json={"state": "pinned"})
check(
    "pinned → pinned (no-op) emits nothing",
    events_for_finding(TRANS_FID) == [*before, "pin"],
)

client.post(f"/api/findings/{TRANS_FID}/view", json={"state": "dismissed"})
check(
    "pinned → dismissed emits unpin + dismiss",
    events_for_finding(TRANS_FID) == [*before, "pin", "unpin", "dismiss"],
)

client.post(f"/api/findings/{TRANS_FID}/view", json={"state": "seen"})
check(
    "dismissed → seen emits undismiss",
    events_for_finding(TRANS_FID) == [*before, "pin", "unpin", "dismiss", "undismiss"],
)

client.post(
    f"/api/findings/{TRANS_FID}/view",
    json={"state": "snoozed", "snoozed_until": (datetime.utcnow() + timedelta(days=7)).isoformat()},
)
with Session(engine) as s:
    last_snooze = (
        s.query(models.UserSignalEvent)
        .filter_by(finding_id=TRANS_FID, event_type="snooze")
        .order_by(models.UserSignalEvent.id.desc())
        .first()
    )
check(
    "snooze event carries snoozed_until in meta",
    last_snooze is not None and "snoozed_until" in (last_snooze.meta or {}),
)


# ────────────────────────── §3: Transactional rollback ────────────────
section("SignalView + event dual-write rollback")

# Use a fresh finding so we can observe no SignalView + no event side-effect.
ROLLBACK_FID = FINDING_IDS[4]


def signal_view_count(fid: int) -> int:
    with Session(engine) as s:
        return (
            s.query(models.SignalView)
            .filter_by(user_id=USER_ID, finding_id=fid)
            .count()
        )


def events_count_for(fid: int) -> int:
    with Session(engine) as s:
        return (
            s.query(models.UserSignalEvent)
            .filter_by(finding_id=fid)
            .count()
        )


initial_sv = signal_view_count(ROLLBACK_FID)
initial_ev = events_count_for(ROLLBACK_FID)

# Simulate failure of the event insert inside the transaction by raising
# from a before_flush hook when a UserSignalEvent is pending. Any INSERT
# already flushed in the same commit() must be rolled back.
from sqlalchemy import event as sa_event  # noqa: E402
from sqlalchemy.orm import Session as OrmSession  # noqa: E402


def _fail_on_event_flush(session, flush_context, instances):
    for obj in session.new:
        if isinstance(obj, models.UserSignalEvent):
            raise RuntimeError("simulated event flush failure")


# TestClient re-raises server-side exceptions by default — disable that
# for this one call so we can assert post-failure DB state instead.
rollback_client = TestClient(app, raise_server_exceptions=False)
rollback_client.cookies.set(SESSION_COOKIE, token)

sa_event.listen(OrmSession, "before_flush", _fail_on_event_flush)
try:
    r = rollback_client.post(f"/api/findings/{ROLLBACK_FID}/view", json={"state": "pinned"})
finally:
    sa_event.remove(OrmSession, "before_flush", _fail_on_event_flush)

check(
    "failed event insert rolls back SignalView write",
    signal_view_count(ROLLBACK_FID) == initial_sv,
    f"SV count: {signal_view_count(ROLLBACK_FID)} (was {initial_sv})",
)
check(
    "failed event insert does not leave a partial event row",
    events_count_for(ROLLBACK_FID) == initial_ev,
)

# Confirm the route still works after recovery — no leaked state.
r = client.post(f"/api/findings/{ROLLBACK_FID}/view", json={"state": "pinned"})
check(
    "post-rollback recovery: pin writes both SV + event",
    r.status_code == 200
    and signal_view_count(ROLLBACK_FID) == initial_sv + 1
    and events_count_for(ROLLBACK_FID) == initial_ev + 1,
)


# ────────────────────────── §4: Retention prune ───────────────────────
section("prune_signal_events retention")

with Session(engine) as s:
    # Seed a known set of aged rows on a fresh finding.
    f = models.Finding(competitor="Y", source="test", hash="prune-h")
    s.add(f)
    s.commit()
    s.refresh(f)
    now = datetime.utcnow()
    for age in (0, 30, 179, 181, 365):
        s.add(models.UserSignalEvent(
            user_id=USER_ID, finding_id=f.id, event_type="view",
            source="stream", ts=now - timedelta(days=age),
            meta={"age": age},
        ))
    s.commit()
    prune_fid = f.id

before_count = events_count_for(prune_fid)
deleted = prune_signal_events(retention_days=180)
after_count = events_count_for(prune_fid)
check(
    "prune deletes rows older than retention window",
    deleted >= 2 and after_count == before_count - 2,
    f"deleted={deleted} before={before_count} after={after_count}",
)

# Boundary: the 179d row must still be present; the 181d one must not.
with Session(engine) as s:
    ages_remaining = sorted(
        (e.meta or {}).get("age", -1)
        for e in s.query(models.UserSignalEvent).filter_by(finding_id=prune_fid).all()
    )
check(
    "179d row survives, 181d+ are gone",
    179 in ages_remaining and 181 not in ages_remaining and 365 not in ages_remaining,
    f"ages left: {ages_remaining}",
)


# ────────────────────────── §5: Index coverage ────────────────────────
section("Index coverage for rollup query")

with engine.connect() as conn:
    plan_rows = conn.execute(text(
        "EXPLAIN QUERY PLAN "
        "SELECT * FROM user_signal_events "
        f"WHERE user_id = {USER_ID} "
        "AND ts >= datetime('now', '-30 days') "
        "ORDER BY ts DESC"
    )).fetchall()
plan_text = "\n".join(str(r) for r in plan_rows)
check(
    "rollup query uses ix_user_signal_events_user_ts",
    "ix_user_signal_events_user_ts" in plan_text,
    plan_text.replace("\n", " | "),
)


# ────────────────────────── §6: Schema shape ──────────────────────────
section("Schema shape")

insp = inspect(engine)
cols = {c["name"] for c in insp.get_columns("user_signal_events")}
expected_cols = {"id", "user_id", "finding_id", "event_type", "value", "source", "meta", "ts"}
check("user_signal_events has all expected columns", cols == expected_cols, f"diff: {cols ^ expected_cols}")

idx_names = {i["name"] for i in insp.get_indexes("user_signal_events")}
check(
    "expected indexes exist",
    {"ix_user_signal_events_user_ts", "ix_user_signal_events_user_type_ts",
     "ix_user_signal_events_finding"}.issubset(idx_names),
    f"present: {sorted(idx_names)}",
)

fks = {fk["referred_table"]: fk.get("options", {}).get("ondelete") for fk in insp.get_foreign_keys("user_signal_events")}
check(
    "FK cascade rules match spec",
    fks.get("users") == "CASCADE" and fks.get("findings") == "SET NULL",
    f"fks: {fks}",
)


# ────────────────────────── Summary ──────────────────────────────────

print()
print(f"Total: {_passes} passed, {len(_failures)} failed")
if _failures:
    print("Failed checks:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
sys.exit(0)
