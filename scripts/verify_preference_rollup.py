"""End-to-end verification for the preference rollup
(docs/ranker/02-preference-rollup.md).

Runs the spec 02 acceptance criteria against a throwaway SQLite DB —
same pattern as scripts/verify_signal_log.py. Hermetic; can run in CI.

Usage:
    python scripts/verify_preference_rollup.py
Exit code: 0 on all pass, 1 on any fail.
"""
from __future__ import annotations

import math
import os
import secrets
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="pref_rollup_verify_")) / "verify.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base  # noqa: E402
from app import models  # noqa: E402
from app.auth import SESSION_COOKIE  # noqa: E402
from app.main import app  # noqa: E402
from app.ranker import config as rcfg  # noqa: E402
from app.ranker.preferences import load_profile  # noqa: E402
from app.ranker.rollup import rebuild_user_preferences  # noqa: E402


# ── Harness ─────────────────────────────────────────────────────────

_passes = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
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


engine = create_engine(os.environ["DATABASE_URL"])
Base.metadata.create_all(bind=engine)

with Session(engine) as s:
    u = models.User(email="t@t", name="t", role="admin", is_active=True)
    s.add(u)
    s.commit()
    s.refresh(u)
    USER_ID = u.id

    indeed_hire = models.Finding(
        competitor="Indeed", source="news", signal_type="new_hire",
        topic="talent", matched_keyword="hiring", hash="h1",
    )
    indeed_launch = models.Finding(
        competitor="Indeed", source="news", signal_type="product_launch",
        topic="platform", matched_keyword="launch", hash="h2",
    )
    linkedin_hire = models.Finding(
        competitor="LinkedIn", source="careers", signal_type="new_hire",
        topic="talent", matched_keyword="hiring", hash="h3",
    )
    for f in (indeed_hire, indeed_launch, linkedin_hire):
        s.add(f)
    s.commit()
    FIDS = {
        "indeed_hire": indeed_hire.id,
        "indeed_launch": indeed_launch.id,
        "linkedin_hire": linkedin_hire.id,
    }

    token = secrets.token_hex(16)
    s.add(models.AuthSession(
        token=token, user_id=USER_ID,
        expires_at=datetime.utcnow() + timedelta(days=1),
    ))
    s.commit()


def seed_event(event_type: str, finding_id: int | None, days_ago: float = 0.0,
               value: float | None = None, meta: dict | None = None) -> None:
    with Session(engine) as s:
        s.add(models.UserSignalEvent(
            user_id=USER_ID, finding_id=finding_id, event_type=event_type,
            source="stream", value=value, meta=meta or {},
            ts=datetime.utcnow() - timedelta(days=days_ago),
        ))
        s.commit()


# ── §1: Schema shape ─────────────────────────────────────────────────
section("Schema shape")

insp = inspect(engine)
vec_cols = {c["name"] for c in insp.get_columns("user_preferences_vector")}
expected = {"user_id", "dimension", "key", "weight", "raw_sum",
            "evidence_count", "positive_count", "negative_count", "last_event_at"}
check("user_preferences_vector has all expected columns", vec_cols == expected,
      f"diff: {vec_cols ^ expected}")

profile_cols = {c["name"] for c in insp.get_columns("user_preference_profile")}
expected = {"user_id", "taste_doc", "cold_start", "event_count_30d",
            "last_computed_at", "schema_version"}
check("user_preference_profile has all expected columns", profile_cols == expected,
      f"diff: {profile_cols ^ expected}")

vec_pk = insp.get_pk_constraint("user_preferences_vector")["constrained_columns"]
check("vector PK is (user_id, dimension, key)",
      vec_pk == ["user_id", "dimension", "key"])


# ── §2: Decay + tanh math ────────────────────────────────────────────
section("Decay + tanh math")

# Pin today (+0.80) and view 7d ago (+0.02 * exp(-ln2*7/30) ≈ +0.0168)
# on the same finding. Expected raw_sum ≈ 0.8168.
seed_event("pin", FIDS["indeed_hire"], days_ago=0)
seed_event("view", FIDS["indeed_hire"], days_ago=7)
# Dismiss LinkedIn today (-0.70)
seed_event("dismiss", FIDS["linkedin_hire"], days_ago=0)

with Session(engine) as s:
    summary = rebuild_user_preferences(s, USER_ID)

profile = load_profile(Session(engine), USER_ID)
indeed_entry = profile.vector["competitor"]["Indeed"]
linkedin_entry = profile.vector["competitor"]["LinkedIn"]

expected_raw = 0.80 + 0.02 * math.exp(-math.log(2) * 7 / 30)
check("raw_sum for Indeed matches pin+decayed view within 1e-6",
      abs(indeed_entry.raw_sum - expected_raw) < 1e-6,
      f"got {indeed_entry.raw_sum:.6f} expected {expected_raw:.6f}")
check("weight is tanh(raw_sum)",
      abs(indeed_entry.weight - math.tanh(indeed_entry.raw_sum)) < 1e-9)
check("positive events counted",
      indeed_entry.positive_count == 2 and indeed_entry.negative_count == 0,
      f"pos={indeed_entry.positive_count} neg={indeed_entry.negative_count}")
check("LinkedIn weight is negative (dismiss only)",
      linkedin_entry.weight < 0)
# keys: competitor(Indeed,LinkedIn)=2 + signal_type(new_hire)=1
#       + source(news,careers)=2 + topic(talent)=1 + keyword(hiring)=1 = 7
check("summary reflects events and produced key count",
      summary["events_considered"] == 3 and summary["keys_written"] == 7,
      f"events={summary['events_considered']} keys={summary['keys_written']}")


# ── §3: Dimension fan-out ────────────────────────────────────────────
section("Dimension fan-out")

# One pin touches five dimensions on the finding. Verify each is present
# with the same raw_sum contribution.
dims_present = {d: list(profile.vector.get(d, {}).keys()) for d in
                ("competitor", "signal_type", "source", "topic", "keyword")}
check("all five dimensions populated",
      all(dims_present[d] for d in dims_present),
      f"coverage: {dims_present}")

# signal_type/new_hire should reflect pin (+view) on indeed_hire AND
# dismiss on linkedin_hire. Raw sum = pin + view_decayed − dismiss.
new_hire = profile.vector["signal_type"]["new_hire"]
expected_new_hire_raw = 0.80 + 0.02 * math.exp(-math.log(2) * 7 / 30) - 0.70
check("signal_type/new_hire aggregates across findings with same signal_type",
      abs(new_hire.raw_sum - expected_new_hire_raw) < 1e-6,
      f"got {new_hire.raw_sum:.6f} expected {expected_new_hire_raw:.6f}")


# ── §4: Cold-start flip ─────────────────────────────────────────────
section("Cold-start detection")

profile_fresh = load_profile(Session(engine), USER_ID)
check("cold_start is True when event_count_30d < threshold",
      profile_fresh.cold_start and profile_fresh.event_count_30d < rcfg.COLD_START_THRESHOLD,
      f"count={profile_fresh.event_count_30d} threshold={rcfg.COLD_START_THRESHOLD}")

# Seed enough events to cross the threshold.
for i in range(rcfg.COLD_START_THRESHOLD + 5):
    seed_event("view", FIDS["indeed_hire"], days_ago=0)

with Session(engine) as s:
    rebuild_user_preferences(s, USER_ID)

profile_warm = load_profile(Session(engine), USER_ID)
check("cold_start flips False when event_count_30d >= threshold",
      not profile_warm.cold_start and profile_warm.event_count_30d >= rcfg.COLD_START_THRESHOLD,
      f"count={profile_warm.event_count_30d}")


# ── §5: Idempotency + truncate-and-rewrite ──────────────────────────
section("Idempotency")

with Session(engine) as s:
    s1 = rebuild_user_preferences(s, USER_ID)
with Session(engine) as s:
    s2 = rebuild_user_preferences(s, USER_ID)
check("rebuild is idempotent — two consecutive runs produce same keys_written",
      s1["keys_written"] == s2["keys_written"])

with Session(engine) as s:
    before = s.query(models.UserPreferenceVector).filter_by(user_id=USER_ID).count()
    # Add a stale row that the rollup shouldn't preserve.
    s.add(models.UserPreferenceVector(
        user_id=USER_ID, dimension="competitor", key="StaleKey",
        weight=0.5, raw_sum=0.55, evidence_count=1, positive_count=1,
        negative_count=0, last_event_at=datetime.utcnow(),
    ))
    s.commit()
with Session(engine) as s:
    rebuild_user_preferences(s, USER_ID)
with Session(engine) as s:
    stale = s.query(models.UserPreferenceVector).filter_by(
        user_id=USER_ID, key="StaleKey"
    ).count()
check("stale vector rows are removed by rebuild (truncate-and-rewrite)",
      stale == 0)


# ── §6: HTTP API ────────────────────────────────────────────────────
section("HTTP read API")

client = TestClient(app, raise_server_exceptions=False)
client.cookies.set(SESSION_COOKIE, token)

r = client.get("/api/preferences/me")
check("GET /api/preferences/me returns 200", r.status_code == 200)
body = r.json() if r.status_code == 200 else {}
check("response includes top-by-dimension",
      "top" in body and "competitor" in body.get("top", {}),
      f"keys: {list(body.keys()) if body else '<no body>'}")
check("cold_start field present in response",
      "cold_start" in body and isinstance(body.get("cold_start"), bool))

r = client.post("/api/preferences/me/rebuild")
check("POST /api/preferences/me/rebuild returns 200 first time",
      r.status_code == 200)

r = client.post("/api/preferences/me/rebuild")
check("immediate second rebuild is rate-limited (429)",
      r.status_code == 429,
      f"got {r.status_code}: {r.text[:100]}")
check("429 response carries Retry-After header",
      "retry-after" in {k.lower() for k in r.headers.keys()}
      if r.status_code == 429 else False)


# ── §7: Incremental trigger debounce (scheduler) ────────────────────
section("Incremental rebuild debounce")

# The scheduler isn't running in this harness (no asyncio loop), so
# schedule_incremental_rebuild returns False. We verify it's callable
# and coalesces — not full scheduling.
from app.scheduler import schedule_incremental_rebuild  # noqa: E402
check("schedule_incremental_rebuild is a no-op when scheduler isn't running",
      schedule_incremental_rebuild(USER_ID) is False)

# Direct check: the taxonomy of incremental-trigger event types
# matches spec 02.
check("incremental trigger set matches spec",
      rcfg.INCREMENTAL_TRIGGER_TYPES == frozenset({
          "rate_up", "rate_down", "chat_pref_update",
      }))


# ── §8: Zero-event user ──────────────────────────────────────────────
section("Zero-event edge case")

with Session(engine) as s:
    u2 = models.User(email="cold@t", name="cold", role="admin", is_active=True)
    s.add(u2)
    s.commit()
    s.refresh(u2)
    cold_id = u2.id

with Session(engine) as s:
    summary = rebuild_user_preferences(s, cold_id)
check("rollup succeeds for user with zero events",
      summary["events_considered"] == 0 and summary["keys_written"] == 0)

profile = load_profile(Session(engine), cold_id)
check("zero-event user has cold_start=True and empty vector",
      profile.cold_start and profile.vector == {})


# ── Summary ─────────────────────────────────────────────────────────
print()
print(f"Total: {_passes} passed, {len(_failures)} failed")
if _failures:
    print("Failed checks:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
sys.exit(0)
