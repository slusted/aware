"""Admin notifications tab (docs/chat/02-scheduled-questions.md
§"Settings & admin placement").

Owns the mail-account credentials surface (Gmail SMTP/IMAP) and the
scheduled-question reply poller's status / controls. Engine API keys
live at /settings/keys; this is everything else under the umbrella
"how messages leave and arrive."
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .. import env_keys, scheduler as scheduler_module
from ..chat import replies as chat_replies
from ..deps import require_role
from ..models import User


router = APIRouter(tags=["notifications"], include_in_schema=False)
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)


def _poll_job_state() -> dict:
    """Live state of the scheduler's chat_reply_poll job, plus the
    last-poll status the poller writes into chat_replies.STATUS."""
    sched = scheduler_module.get()
    job = sched.get_job("chat_reply_poll") if sched else None
    return {
        "registered": job is not None,
        "next_run_at": job.next_run_time if job else None,
        "last_poll_at": chat_replies.STATUS.get("last_poll_at"),
        "last_error": chat_replies.STATUS.get("last_error"),
        "last_processed": chat_replies.STATUS.get("last_processed", 0),
        "last_skipped": chat_replies.STATUS.get("last_skipped", 0),
        "total_processed": chat_replies.STATUS.get("total_processed", 0),
        "imap_configured": chat_replies.imap_configured(),
    }


@router.get("/admin/notifications", response_class=HTMLResponse)
def admin_notifications(
    request: Request,
    user: User = Depends(require_role("admin")),
):
    return templates.TemplateResponse(request, "admin_notifications.html", {
        "user": user,
        "mail_keys": env_keys.status(category="notifications"),
        "poll_state": _poll_job_state(),
        "test_send_result": None,
    })


class TestSendIn(BaseModel):
    to: str


@router.post("/api/admin/notifications/test-send")
def test_send(
    payload: TestSendIn,
    user: User = Depends(require_role("admin")),
):
    """Fire a one-off test email so the admin can verify Gmail creds
    end-to-end without waiting for a scheduled run. Returns the SMTP
    outcome so the page can render success/failure inline."""
    to = (payload.to or "").strip()
    if not to or "@" not in to:
        raise HTTPException(400, "valid recipient email required")

    import mailer  # top-level mailer.py — see docs/chat/02 §run flow
    subject = "[Watch] Test email"
    body = (
        "This is a test email from Competitor Watch's notifications tab.\n"
        "If you received it, your Gmail SMTP credentials are wired up correctly.\n\n"
        "— Sent from /admin/notifications by an admin."
    )
    sent = mailer.send_email(to, subject, body)
    if not sent:
        # mailer.send_email logs the underlying error and returns False
        # for both "no creds" and "SMTP error" cases. Surface a clear
        # message either way.
        if not chat_replies.imap_configured():
            raise HTTPException(400, "Gmail credentials missing — set them above first.")
        raise HTTPException(502, "Test send failed — check the server logs for the SMTP error.")
    return {"ok": True, "sent_to": to}


@router.post("/api/admin/notifications/poll-now")
def poll_now(user: User = Depends(require_role("admin"))):
    """Run the IMAP poll synchronously and return the outcome. Useful
    for debugging "why didn't my reply land?" without waiting for the
    next interval tick."""
    result = chat_replies.poll_replies()
    return {
        "ok": result.get("last_error") is None,
        "processed": result.get("last_processed", 0),
        "skipped": result.get("last_skipped", 0),
        "error": result.get("last_error"),
    }
