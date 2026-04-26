"""Headless turn runner for scheduled chat questions.

When a schedule's cron fires, ``run_scheduled_question(schedule_id)``
creates a fresh ``ChatSession`` owned by the schedule's user, runs one
chat agent turn end-to-end (with writes auto-cancelled — there's no
human present to click Confirm), and fans the agent's final answer out
to every recipient on the schedule via :mod:`mailer`.

Per docs/chat/02-scheduled-questions.md §"Run flow". The session is
preserved in /chat so users can open it, see what tools the agent
called, and ask follow-ups interactively.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ChatMessage, ChatSchedule, ChatSession, User
from . import agent as chat_agent


# Default {{since}} window for a brand-new schedule. Spec §"Placeholder
# substitution": "On the first run, defaults to '7 days ago' so the
# first email isn't empty."
DEFAULT_SINCE_DAYS = 7

# Small SSE parser regex — events come back as
# ``event: <name>\ndata: <json>\n\n``. We split on the blank-line
# boundary and parse the two known fields. Reusing the agent module's
# rendering means there's exactly one shape to track.
_EVENT_RE = re.compile(r"event:\s*(\S+)\s*\n\s*data:\s*(.*)", re.DOTALL)


# ---------- public entry point ----------------------------------------------


def run_scheduled_question(schedule_id: int) -> dict:
    """Cron entrypoint. Always opens its own DB session; safe to call
    from any thread. Returns a small result dict for logging — the
    schedule row itself is updated in-place with the run outcome."""
    db = SessionLocal()
    try:
        schedule = db.get(ChatSchedule, schedule_id)
        if not schedule:
            return {"ok": False, "error": "schedule_not_found"}
        if not schedule.enabled:
            # The cron may have fired between toggle and unregistration;
            # silently skip rather than running anything.
            return {"ok": False, "skipped": "disabled"}

        owner = db.get(User, schedule.user_id)
        if not owner or not owner.is_active:
            _record_failure(db, schedule, "owner_inactive",
                            "Schedule owner is inactive or removed.")
            return {"ok": False, "error": "owner_inactive"}

        return _run(db, schedule, owner)
    finally:
        db.close()


# ---------- core runner ------------------------------------------------------


def _run(db: Session, schedule: ChatSchedule, owner: User) -> dict:
    prompt_text = _substitute(schedule.prompt or "", schedule)
    session = _create_session(db, schedule, owner)

    # Drain the agent's SSE stream into a single final text + status.
    # Auto-cancels any write tool the agent emits (spec §"Auth", "Writes
    # from a scheduled run are auto-cancelled").
    final_text, status, status_error = drive_agent_headless(
        db, session, owner, prompt_text,
    )

    # Build the email body — same shape success or failure so the
    # footer's ref token always points back to a real session.
    if status == "ok":
        subject = f"[Watch Question] {schedule.title} — {datetime.utcnow():%Y-%m-%d}"
        body = (final_text or "").strip() + "\n\n" + _footer(schedule, session)
    else:
        subject = f"[Watch Question] FAILED — {schedule.title}"
        msg = status_error or "unknown error"
        body = (
            f"Scheduled question failed: {msg}\n\n"
            "Open the conversation to inspect what the agent saw before "
            "it errored.\n\n"
            + _footer(schedule, session)
        )

    # Fan out to recipients. Records per-recipient outcome on the row.
    fanout = _fanout_email(schedule.recipient_emails or [], subject, body)

    # Final status precedence: agent failure beats partial-email beats ok.
    if status != "ok":
        final_status = status
    elif fanout["any_failed"] and not fanout["all_failed"]:
        final_status = "partial_email"
    elif fanout["all_failed"] and fanout["attempted"]:
        final_status = "partial_email"  # spec: ≥1 failure → partial_email
    elif fanout["unconfigured"]:
        final_status = "no_email"
    else:
        final_status = "ok"

    schedule.last_run_at = datetime.utcnow()
    schedule.last_session_id = session.id
    schedule.last_status = final_status
    schedule.last_error = (status_error or "")[:500] if status != "ok" else None
    schedule.last_recipient_status = fanout["per_recipient"]
    schedule.updated_at = datetime.utcnow()
    db.commit()

    return {
        "ok": final_status in ("ok", "partial_email", "no_email"),
        "status": final_status,
        "session_id": session.id,
        "recipients": fanout["per_recipient"],
    }


def _create_session(db: Session, schedule: ChatSchedule, owner: User) -> ChatSession:
    title = f"Scheduled: {schedule.title} · {datetime.utcnow():%Y-%m-%d}"
    session = ChatSession(
        user_id=owner.id,
        title=title[:255],
        scheduled_id=schedule.id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


# ---------- placeholder substitution ----------------------------------------


def _substitute(prompt: str, schedule: ChatSchedule) -> str:
    """Replace {{since}} and {{now}}. ISO 8601 UTC.

    {{since}} = previous run's timestamp; on the first run, defaults to
    DEFAULT_SINCE_DAYS days ago so the first email has content."""
    if schedule.last_run_at:
        since_dt = schedule.last_run_at
    else:
        since_dt = datetime.utcnow() - timedelta(days=DEFAULT_SINCE_DAYS)
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return prompt.replace("{{since}}", since_iso).replace("{{now}}", now_iso)


# ---------- agent loop driver -----------------------------------------------


def drive_agent_headless(
    db: Session,
    session: ChatSession,
    owner: User,
    prompt_text: str,
) -> tuple[str, str, str | None]:
    """Run the chat agent to completion (or auto-cancel a write).

    Returns ``(final_text, status, error)`` where ``status`` is one of
    ``"ok"`` / ``"error"`` / ``"timeout"``.

    Used by both the scheduled-run loop and the reply-poll fork loop.
    Auto-cancel loop: if the agent emits ``confirmation_pending`` for a
    write tool, flip every pending tool_use row to ``cancelled`` and
    re-enter ``run_turn`` to drain the cancellation tool_results and
    continue the model loop. Bounded by ``MAX_RESUMES`` to keep a
    pathological agent from looping forever.
    """
    final_text_parts: list[str] = []
    status: str = "error"
    error_msg: str | None = None
    stop_reason: str | None = None

    MAX_RESUMES = 4
    resumes = 0
    user_text: str | None = prompt_text
    saw_confirmation_pending = False

    while True:
        try:
            stream = chat_agent.run_turn(
                db, session, owner, user_text=user_text,
            )
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            status = "error"
            break

        saw_confirmation_pending = False
        for raw in stream:
            event, data = _parse_sse(raw)
            if event == "text_delta":
                final_text_parts.append(data.get("text", ""))
            elif event == "error":
                error_msg = data.get("message") or "unknown error"
                status = "timeout" if "timed out" in error_msg.lower() else "error"
            elif event == "turn_end":
                stop_reason = data.get("stop_reason")
                if stop_reason == "end_turn":
                    status = "ok"
            elif event == "confirmation_pending":
                saw_confirmation_pending = True

        if not saw_confirmation_pending:
            break

        # A write tool's confirmation card is up — we have no human, so
        # cancel everything pending and resume to drain the cancelled
        # tool_results back into the agent loop.
        cancelled = _cancel_pending_tool_uses(db, session.id)
        resumes += 1
        if resumes > MAX_RESUMES or cancelled == 0:
            error_msg = (
                "Agent kept requesting confirmation after auto-cancel; "
                "giving up after %d resume cycles." % MAX_RESUMES
            )
            status = "error"
            break

        # Reset for the next loop iteration: clear final_text_parts so
        # we don't email the partial pre-cancel text — the post-resume
        # turn will produce a coherent answer that acknowledges the
        # cancellation.
        final_text_parts = []
        user_text = None  # resume mode

    final_text = "".join(final_text_parts).strip()

    # Defensive: a status="ok" with zero text shouldn't email an empty
    # body. Promote to error so the recipient sees a useful failure
    # rather than a blank message.
    if status == "ok" and not final_text:
        status = "error"
        error_msg = error_msg or "Agent produced no text."

    return final_text, status, error_msg


def _parse_sse(raw: str) -> tuple[str, dict]:
    """Best-effort SSE parse. Returns ``(event, data_dict)`` — empty
    dict if the data line isn't JSON (e.g. a heartbeat we don't care
    about)."""
    m = _EVENT_RE.match(raw.strip())
    if not m:
        return ("", {})
    event = m.group(1).strip()
    payload = m.group(2).strip()
    try:
        return (event, json.loads(payload))
    except Exception:
        return (event, {})


def _cancel_pending_tool_uses(db: Session, session_id: int) -> int:
    """Flip every confirmation_status='pending' tool_use row in the
    session to 'cancelled'. Returns how many rows changed.

    The agent's resume path (run_turn with user_text=None) will see the
    cancelled rows, persist a tool_result for each, and continue the
    model loop with the cancellation results in context.
    """
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "tool_use",
        )
        .all()
    )
    n = 0
    for row in rows:
        pl = dict(row.tool_payload or {})
        if pl.get("confirmation_status") != "pending":
            continue
        pl["confirmation_status"] = "cancelled"
        pl["cancelled_reason"] = "scheduled run, no interactive confirmation"
        row.tool_payload = pl
        n += 1
    if n:
        db.commit()
    return n


# ---------- email -----------------------------------------------------------


def _footer(schedule: ChatSchedule, session: ChatSession) -> str:
    """Per spec §"Footer template". The ``[ref:s{id}]`` token is the
    canonical routing key for replies — the IMAP poller pulls it out
    of the quoted body to know which session the reply continues."""
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    return (
        "---\n"
        f"From your scheduled question: {schedule.title}\n"
        "Open the conversation to see what the agent looked at, or ask a follow-up:\n"
        f"{base}/chat/{session.id}\n\n"
        "Reply to this email to ask a follow-up question — the agent will "
        f"reply to you (only). [ref:s{session.id}]\n\n"
        f"Manage this schedule: {base}/chat/schedules\n"
    )


def _fanout_email(recipients: Iterable[str], subject: str, body: str) -> dict:
    """Send the same body to every recipient. Returns a result map
    plus rolled-up booleans the caller uses to pick the schedule's
    final last_status."""
    import mailer  # top-level

    per_recipient: dict[str, str] = {}
    attempted = 0
    succeeded = 0
    failed = 0
    unconfigured = not (mailer.GMAIL_USER and mailer.GMAIL_PASSWORD)

    if unconfigured:
        # Don't even try; mark every recipient as no_email so the
        # dashboard shows the cause clearly. Avoids per-recipient SMTP
        # errors that would all say the same thing.
        for addr in (recipients or []):
            per_recipient[addr] = "no_email"
        return {
            "per_recipient": per_recipient,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "any_failed": False,
            "all_failed": False,
            "unconfigured": True,
        }

    for raw in (recipients or []):
        addr = (raw or "").strip()
        if not addr:
            continue
        attempted += 1
        try:
            ok = mailer.send_email(addr, subject, body)
        except Exception as e:
            ok = False
            per_recipient[addr] = f"error: {type(e).__name__}: {e}"[:200]
        else:
            per_recipient[addr] = "ok" if ok else "error: send_email returned False"
        if ok:
            succeeded += 1
        else:
            failed += 1

    return {
        "per_recipient": per_recipient,
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "any_failed": failed > 0,
        "all_failed": failed > 0 and succeeded == 0,
        "unconfigured": False,
    }


# ---------- failure recording for non-runner errors -------------------------


def _record_failure(
    db: Session,
    schedule: ChatSchedule,
    code: str,
    message: str,
) -> None:
    """Stamp a failure on the schedule when we can't even start a run
    (e.g. owner is inactive). No ChatSession is created in this case —
    last_session_id stays whatever it was."""
    schedule.last_run_at = datetime.utcnow()
    schedule.last_status = "error"
    schedule.last_error = (f"{code}: {message}")[:500]
    schedule.last_recipient_status = {}
    schedule.updated_at = datetime.utcnow()
    db.commit()
