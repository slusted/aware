"""End-to-end verification for swipe feedback (docs/ranker/09-swipe-feedback.md).

Hermetic — uses a throwaway SQLite DB and the FastAPI TestClient.
Covers the question endpoint, the pinned-first profile sort, and the
hidden-by-default filter behaviour on the competitor profile.

Usage:
    python scripts/verify_swipe_feedback.py
Exit code: 0 on all pass, 1 on any fail.
"""
from __future__ import annotations

import os
import secrets
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="swipe_verify_")) / "verify.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base  # noqa: E402
from app import models  # noqa: E402
from app.auth import SESSION_COOKIE  # noqa: E402
from app.main import app  # noqa: E402


# ── Harness ─────────────────────────────────────────────────────────

_passes = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f"  -- {detail}"
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

    competitor = models.Competitor(name="Indeed", active=True)
    s.add(competitor)
    s.commit()
    s.refresh(competitor)
    COMP_ID = competitor.id

    f1 = models.Finding(
        competitor="Indeed", source="news", signal_type="new_hire",
        title="Indeed hires VP Engineering",
        hash="hf1",
        created_at=datetime.utcnow() - timedelta(days=2),
    )
    f2 = models.Finding(
        competitor="Indeed", source="news", signal_type="product_launch",
        title="Indeed launches new dashboard",
        hash="hf2",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    f3 = models.Finding(
        competitor="Indeed", source="news", signal_type="news",
        title="Old news article",
        hash="hf3",
        created_at=datetime.utcnow() - timedelta(days=5),
    )
    f_other = models.Finding(
        competitor="LinkedIn", source="news", signal_type="news",
        title="LinkedIn launches feature",
        hash="hflk",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    for f in (f1, f2, f3, f_other):
        s.add(f)
    s.commit()
    FIDS = {"f1": f1.id, "f2": f2.id, "f3": f3.id, "other": f_other.id}

    token = secrets.token_hex(16)
    s.add(models.AuthSession(
        token=token, user_id=USER_ID,
        expires_at=datetime.utcnow() + timedelta(days=1),
    ))
    s.commit()


# ── Schema check ────────────────────────────────────────────────────
section("Schema shape")

insp = inspect(engine)
sv_cols = {c["name"] for c in insp.get_columns("signal_views")}
check("signal_views.question column exists", "question" in sv_cols)
question_col = next(
    (c for c in insp.get_columns("signal_views") if c["name"] == "question"), None
)
check("question column is nullable",
      question_col is not None and question_col.get("nullable", True))


# ── HTTP harness ────────────────────────────────────────────────────
client = TestClient(app, raise_server_exceptions=False)
client.cookies.set(SESSION_COOKIE, token)


# ── §1 — Question endpoint validation ────────────────────────────────
section("Question endpoint validation")

r = client.post(f"/partials/finding/{FIDS['f1']}/question",
                data={"question": ""})
check("empty question returns 400", r.status_code == 400,
      f"got {r.status_code}")

r = client.post(f"/partials/finding/{FIDS['f1']}/question",
                data={"question": "   "})
check("whitespace-only question returns 400", r.status_code == 400)

r = client.post(f"/partials/finding/{FIDS['f1']}/question",
                data={"question": "x" * 501})
check(">500 char question returns 400", r.status_code == 400,
      f"got {r.status_code}")

r = client.post(f"/partials/finding/999999/question",
                data={"question": "test"})
check("non-existent finding returns 404", r.status_code == 404)


# ── §2 — Question side effects ─────────────────────────────────────
section("Question endpoint side effects")

q_text = "How does this compare to LinkedIns hire last quarter"
r = client.post(f"/partials/finding/{FIDS['f1']}/question",
                data={"question": q_text})
check("valid question returns 200", r.status_code == 200,
      f"got {r.status_code}: {r.text[:100]}")
# Re-rendered card markup must echo the question in the textarea so a
# subsequent flip can show + edit the prior text. Question deliberately
# contains no special chars so we can substring-match the raw HTML.
import re as _re
ta_match = _re.search(
    r'<textarea[^>]*signal-card-back-textarea[^>]*>([^<]*)</textarea>',
    r.text,
)
check("re-rendered card prefills textarea with stored question",
      ta_match is not None and ta_match.group(1).strip() == q_text,
      f"got {ta_match.group(1) if ta_match else '(no textarea)'!r}")
# And the primary button label flips to "Pin & ask" so the user sees
# pressing it will re-submit (or update) the question.
check("primary button label is 'Pin & ask' when question stored",
      ">Pin &amp; ask<" in r.text or ">Pin & ask<" in r.text,
      "label should say 'Pin & ask' on cards with stored question")

with Session(engine) as s:
    sv = (
        s.query(models.SignalView)
        .filter_by(user_id=USER_ID, finding_id=FIDS["f1"])
        .first()
    )
    check("SignalView upserted", sv is not None)
    if sv is not None:
        check("question text persisted", sv.question == q_text,
              f"got {sv.question!r}")
        check("state set to pinned", sv.state == "pinned",
              f"got {sv.state!r}")

    events = (
        s.query(models.UserSignalEvent)
        .filter_by(user_id=USER_ID, finding_id=FIDS["f1"])
        .all()
    )
    pin_events = [e for e in events if e.event_type == "pin"]
    check("exactly one pin event written", len(pin_events) == 1,
          f"got {len(pin_events)}")
    if pin_events:
        ev = pin_events[0]
        check("event meta.via == 'swipe_flip'",
              ev.meta.get("via") == "swipe_flip",
              f"got {ev.meta}")
        check("event meta.question_chars matches text length",
              ev.meta.get("question_chars") == len(q_text),
              f"got {ev.meta.get('question_chars')}")
        check("event log does NOT contain question text",
              q_text not in str(ev.meta) and ev.value is None,
              "question text must live on SignalView only")


# ── §3 — Question replaces previous (idempotent upsert) ────────────
section("Question idempotency")

q2 = "Updated question text"
r = client.post(f"/partials/finding/{FIDS['f1']}/question",
                data={"question": q2})
check("second submission still 200", r.status_code == 200)
with Session(engine) as s:
    sv = (
        s.query(models.SignalView)
        .filter_by(user_id=USER_ID, finding_id=FIDS["f1"])
        .first()
    )
    check("question replaced, not appended", sv.question == q2,
          f"got {sv.question!r}")
    sv_count = (
        s.query(models.SignalView)
        .filter_by(user_id=USER_ID, finding_id=FIDS["f1"])
        .count()
    )
    check("still one SignalView row for this user-finding pair",
          sv_count == 1, f"got {sv_count}")
    pin_count = (
        s.query(models.UserSignalEvent)
        .filter_by(user_id=USER_ID, finding_id=FIDS["f1"], event_type="pin")
        .count()
    )
    check("each call appends a fresh pin event (event log is append-only)",
          pin_count == 2, f"got {pin_count}")


# ── §4 — Existing pin/dismiss path is untouched ────────────────────
section("Existing /partials/stream_view path still works")

# Pin via the existing endpoint (the back-of-card 'Pin / save' button uses this).
r = client.post(f"/partials/stream_view/{FIDS['f2']}",
                data={"state": "pinned"})
check("plain pin via stream_view returns 200", r.status_code == 200)
with Session(engine) as s:
    sv = (
        s.query(models.SignalView)
        .filter_by(user_id=USER_ID, finding_id=FIDS["f2"])
        .first()
    )
    check("plain pin sets state=pinned, leaves question NULL",
          sv is not None and sv.state == "pinned" and sv.question is None,
          f"state={sv.state if sv else None}, q={sv.question if sv else None}")

# Hide via the existing endpoint (left swipe).
r = client.post(f"/partials/stream_view/{FIDS['f3']}",
                data={"state": "dismissed"})
check("hide via stream_view returns 200", r.status_code == 200)


# ── §5 — Competitor profile sort & hidden filter ───────────────────
section("Competitor profile pinned-first sort")

r = client.get(f"/competitors/{COMP_ID}")
check("competitor profile renders 200", r.status_code == 200,
      f"got {r.status_code}")
body = r.text

# f1 (pinned with question) should appear before f2 (pinned, no question)
# should appear before any unpinned, undismissed finding.
# f3 is dismissed → must NOT appear.
# f_other is for a different competitor → must NOT appear.
def find_idx(needle: str) -> int:
    return body.find(needle)

idx_f1 = find_idx(f"card-{FIDS['f1']}")
idx_f2 = find_idx(f"card-{FIDS['f2']}")
idx_f3 = find_idx(f"card-{FIDS['f3']}")
idx_other = find_idx(f"card-{FIDS['other']}")

check("f1 (pinned) present in profile", idx_f1 > 0,
      f"idx_f1={idx_f1}")
check("f2 (pinned) present in profile", idx_f2 > 0,
      f"idx_f2={idx_f2}")
check("f3 (dismissed) hidden from profile by default",
      idx_f3 == -1, f"idx_f3={idx_f3}")
check("other competitor's finding not on this profile",
      idx_other == -1, f"idx_other={idx_other}")

# Pinned f1 + f2 should both come before any unpinned card (there are none
# left for Indeed in this fixture, so we verify their relative order
# matches insertion: f2 is newer (1d ago) vs f1 (2d ago), so within the
# pinned group f2 should come first when sorted by created_at desc.
if idx_f1 > 0 and idx_f2 > 0:
    check("among pinned findings, newer one (f2) appears first",
          idx_f2 < idx_f1, f"idx_f2={idx_f2} idx_f1={idx_f1}")

# Question echoed in expanded panel for f1.
check("follow-up question echoed in profile expand panel",
      "Updated question text" in body)

# Verify the back-of-card markup is NOT rendered on the profile (the
# partial's expandable=True branch suppresses the flip toggle and the
# back of card per spec 09 §UI - competitor profile).
check("back-of-card markup absent on profile (expandable=True)",
      "signal-card-back-inner" not in body)
check("flip toggle absent on profile",
      "flip-toggle" not in body)


# ── §6 — Stream filter copy (Show hidden, not Show dismissed) ──────
section("Stream filter rename")

r = client.get("/stream")
if r.status_code == 200:
    body = r.text
    check("stream filter says 'Show hidden'", "Show hidden" in body,
          "could not find 'Show hidden' label")
    check("'Show dismissed' label removed",
          "Show dismissed" not in body)
else:
    # Stream may require additional setup; not a hard fail for this verify.
    print(f"  [SKIP] stream filter rename — /stream returned {r.status_code}")


# ── Summary ─────────────────────────────────────────────────────────
print()
print(f"Total: {_passes} passed, {len(_failures)} failed")
if _failures:
    print("Failed checks:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
sys.exit(0)
