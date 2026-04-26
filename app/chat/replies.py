"""IMAP poll for replies to scheduled-question emails.

A scheduler job (registered in :mod:`app.scheduler`) calls
:func:`poll_replies` on a fixed interval. The poller scans the Gmail
inbox for unseen messages whose subject starts with
``Re: [Watch Question]`` and routes recognised-sender replies into a
forked or continued chat session per docs/chat/02-scheduled-questions.md
§"Reply-to-converse".

Routing rules (spec §"Routing rules"):
- Sender email matches a row in ``users.email`` (case-insensitive). If
  not, mark the message Seen, log, return — never email back. (Replying
  to an unrecognised sender would be a spoof megaphone.)
- Body or quoted body contains ``[ref:s<digits>]`` — the canonical
  routing key. The ``s`` prefix scopes the namespace; future shapes
  (e.g. ``[ref:f<id>]``) won't collide.
- Look up the referenced ChatSession; skip if archived/missing.
- If the session is owned by the replier → continue (it's already a
  fork). Otherwise → fork: copy messages into a new session owned by
  the replier, append the reply, run the agent, email back.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import datetime
from email.utils import parseaddr
from typing import Any

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ChatMessage, ChatSchedule, ChatSession, User


# Live status read by /admin/notifications. Updated on every poll, win
# or lose, so the dashboard reflects the most recent attempt.
STATUS: dict[str, Any] = {
    "last_poll_at": None,        # datetime | None
    "last_error": None,          # str | None — exception text, blank on success
    "last_processed": 0,         # int — replies routed in the most recent poll
    "last_skipped": 0,           # int — unseen Re: messages we touched but ignored
    "total_processed": 0,        # int — running counter since process start
}


SUBJECT_PREFIX = "[Watch Question]"
_REF_RE = re.compile(r"\[ref:s(\d+)\]")


def imap_configured() -> bool:
    return bool(os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"))


# ---------- entry point ------------------------------------------------------


def poll_replies() -> dict:
    """Scan the inbox once and route any matching replies.

    Returns a dict mirroring ``STATUS`` for the caller (the admin
    "Poll now" button reads this directly). Never raises — any IMAP
    or routing error is captured into ``STATUS["last_error"]``.
    """
    STATUS["last_poll_at"] = datetime.utcnow()
    STATUS["last_processed"] = 0
    STATUS["last_skipped"] = 0

    if not imap_configured():
        STATUS["last_error"] = "IMAP not configured (GMAIL_USER/GMAIL_APP_PASSWORD unset)"
        return dict(STATUS)

    user = os.environ.get("GMAIL_USER", "")
    pwd = os.environ.get("GMAIL_APP_PASSWORD", "")
    processed = 0
    skipped = 0

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(user, pwd)
        try:
            mail.select("inbox")
            status, data = mail.search(None, f'(UNSEEN SUBJECT "{SUBJECT_PREFIX}")')
            if status != "OK" or not data or not data[0]:
                STATUS["last_error"] = None
                return dict(STATUS)

            for msg_id in data[0].split():
                outcome = _handle_one(mail, msg_id)
                if outcome == "processed":
                    processed += 1
                elif outcome == "skipped":
                    skipped += 1
                # Mark seen regardless — never re-process the same reply.
                try:
                    mail.store(msg_id, "+FLAGS", "\\Seen")
                except Exception as e:
                    print(f"  [replies] failed to flag {msg_id} seen: {e}")
        finally:
            try:
                mail.logout()
            except Exception:
                pass
    except Exception as e:
        STATUS["last_error"] = f"{type(e).__name__}: {e}"
        return dict(STATUS)

    STATUS["last_error"] = None
    STATUS["last_processed"] = processed
    STATUS["last_skipped"] = skipped
    STATUS["total_processed"] = STATUS.get("total_processed", 0) + processed
    return dict(STATUS)


# ---------- per-message handler ---------------------------------------------


def _handle_one(mail: imaplib.IMAP4_SSL, msg_id: bytes) -> str:
    """Process one IMAP message. Returns ``"processed"`` if a reply was
    routed (forked or continued + emailed), ``"skipped"`` if the
    message was unrecognised / unrouteable / broken, or ``"error"`` if
    something blew up. The caller marks it Seen either way."""
    try:
        status, msg_data = mail.fetch(msg_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            return "skipped"
        msg = email.message_from_bytes(msg_data[0][1])

        subject = (msg.get("Subject") or "")
        if not subject.lower().startswith("re:"):
            return "skipped"

        from_addr = (parseaddr(msg.get("From", ""))[1] or "").strip().lower()
        if not from_addr:
            return "skipped"

        body = _extract_body(msg)
        ref_match = _REF_RE.search(body)
        if not ref_match:
            print(f"  [replies] no ref token in reply from {from_addr}; skipping")
            return "skipped"
        ref_session_id = int(ref_match.group(1))

        # Strip quoted text after token extraction so the reply text the
        # agent sees is just what the user actually typed on top.
        reply_text = _strip_quoted(body)
        if not reply_text:
            print(f"  [replies] empty reply body from {from_addr}; skipping")
            return "skipped"
    except Exception as e:
        print(f"  [replies] parse error on {msg_id}: {e}")
        return "skipped"

    db = SessionLocal()
    try:
        replier = (
            db.query(User)
            .filter(User.email.ilike(from_addr))
            .first()
        )
        if not replier or not replier.is_active:
            print(f"  [replies] ignoring reply from unrecognised address: {from_addr}")
            return "skipped"

        original = db.get(ChatSession, ref_session_id)
        if not original or original.status != "active":
            print(f"  [replies] referenced session s{ref_session_id} missing/archived; skipping")
            return "skipped"

        target = _route(db, original, replier)
        try:
            _append_reply_and_run(db, target, replier, reply_text)
        except Exception as e:
            print(f"  [replies] agent run failed for fork s{target.id}: {e}")
            return "error"
        return "processed"
    finally:
        db.close()


# ---------- routing: fork-or-continue ---------------------------------------


def _route(db: Session, original: ChatSession, replier: User) -> ChatSession:
    """Per spec §"Routing rules":

    - Case A: replier is *not* the session owner (or the session is
      somebody else's fork). Fork: new session owned by the replier,
      copy prior messages, return it.
    - Case B: replier IS the session owner (the replier already
      forked once and is replying again). Continue in place.
    """
    if original.user_id == replier.id:
        return original
    return _fork_session(db, original, replier)


def _fork_session(
    db: Session, original: ChatSession, replier: User,
) -> ChatSession:
    """Copy ``original`` into a new ChatSession owned by ``replier``.

    Carries over every message row in order so the agent sees full
    context for the follow-up. Cost ledger resets — the historical
    tokens are not the replier's spend. The fork's ``forked_from_id``
    points back at ``original`` so the schedule owner's dashboard can
    surface "N follow-up replies this week" cheaply.
    """
    fork = ChatSession(
        user_id=replier.id,
        title=f"Re: {original.title}"[:255],
        status="active",
        model=original.model,
        forked_from_id=original.id,
    )
    db.add(fork)
    db.commit()
    db.refresh(fork)

    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == original.id)
        .order_by(ChatMessage.id.asc())
        .all()
    )
    for row in rows:
        db.add(ChatMessage(
            session_id=fork.id,
            role=row.role,
            content=row.content,
            tool_payload=dict(row.tool_payload or {}),
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            cost_usd=None,
            stop_reason=row.stop_reason,
        ))
    db.commit()
    db.refresh(fork)
    return fork


def _append_reply_and_run(
    db: Session,
    session: ChatSession,
    replier: User,
    reply_text: str,
) -> None:
    """Run one chat turn with the reply as the new user message, then
    email the agent's final answer back to the replier with a footer
    re-keyed to *this* session (so further replies continue here
    rather than re-forking).

    Imports inside the function to keep this module's import surface
    light (replies.py is touched by app.scheduler at boot before chat
    is otherwise needed).
    """
    from . import scheduled as scheduled_module  # local — avoid import cycle
    import mailer  # top-level

    final_text, status, error_msg = scheduled_module.drive_agent_headless(
        db, session, replier, reply_text,
    )

    # Build the reply email. Ref token points at the fork so the next
    # reply in the thread continues here rather than re-forking against
    # the original scheduled session.
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    parent_title = (session.title or "").strip()
    if status == "ok":
        subject = f"Re: [Watch Question] {parent_title}"
        body = (
            (final_text or "").strip()
            + "\n\n---\n"
            f"Continuing your scheduled-question follow-up: {parent_title}\n"
            f"Open the conversation: {base}/chat/{session.id}\n\n"
            "Reply to this email to keep going. "
            f"[ref:s{session.id}]\n"
        )
    else:
        subject = f"Re: [Watch Question] FAILED — {parent_title}"
        body = (
            f"Your follow-up failed: {error_msg or 'unknown error'}\n\n"
            f"Open the conversation to inspect: {base}/chat/{session.id}\n\n"
            f"[ref:s{session.id}]\n"
        )

    if not (mailer.GMAIL_USER and mailer.GMAIL_PASSWORD):
        # Should never happen — IMAP couldn't have logged in if SMTP
        # creds were missing — but be defensive so we don't blow up.
        print(f"  [replies] SMTP unconfigured; can't reply to {replier.email}")
        return
    try:
        mailer.send_email(replier.email, subject, body)
    except Exception as e:
        print(f"  [replies] reply send_email failed for {replier.email}: {e}")


# ---------- body / quote helpers --------------------------------------------


def _extract_body(msg: email.message.Message) -> str:
    """Pull text/plain if available, else strip text/html. Mirrors
    :func:`mailer.check_for_replies`'s approach so the two paths
    behave identically on the same message shapes.
    """
    plain = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not plain:
                payload = part.get_payload(decode=True)
                if payload:
                    plain = payload.decode("utf-8", errors="ignore")
            elif ctype == "text/html" and not html:
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode("utf-8", errors="ignore")
    else:
        payload = msg.get_payload(decode=True) or b""
        decoded = payload.decode("utf-8", errors="ignore")
        if msg.get_content_type() == "text/html":
            html = decoded
        else:
            plain = decoded

    if plain.strip():
        return plain
    if html:
        # Reuse mailer's stripper rather than rolling our own.
        from mailer import _strip_html
        return _strip_html(html)
    return ""


def _strip_quoted(body: str) -> str:
    """Strip the quoted reply tail and any leading "On ... wrote:" line.

    Mirrors :func:`mailer.check_for_replies`: drop "On ... wrote:" and
    everything after it; drop ``>``-prefixed lines. The token may live
    inside the quoted block (that's where it actually arrives — clients
    quote the original body in replies), so we extract the token from
    the *full* body before this strip; this function returns just the
    user's reply text, suitable for sending to the agent.
    """
    lines = body.split("\n")
    clean: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("On ") and "wrote:" in stripped:
            break
        if stripped.startswith(">"):
            continue
        clean.append(line)
    return "\n".join(clean).strip()
