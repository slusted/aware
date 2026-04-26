"""HTTP surface for scheduled chat questions
(docs/chat/02-scheduled-questions.md).

Mixes HTML form posts (create / edit / delete) and small JSON
endpoints (Run now / Toggle) so the dashboard can use ``fetch`` for
the inline buttons without a full page reload, while the form pages
stay server-rendered with no client-side state.

All routes are user-scoped — each one checks
``schedule.user_id == current_user.id`` and 404s otherwise so we
never leak existence across users.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import scheduler as scheduler_module
from ..deps import get_current_user, get_db
from ..models import ChatSchedule, ChatSession, User


router = APIRouter(tags=["chat-schedules"], include_in_schema=False)
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)


# ---------- limits ----------------------------------------------------------


MAX_RECIPIENTS = int(os.environ.get("CHAT_SCHEDULE_MAX_RECIPIENTS", "25"))
MIN_INTERVAL_MINUTES = int(
    os.environ.get("CHAT_SCHEDULE_MIN_INTERVAL_MINUTES", "10")
)

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


# ---------- preset crons surfaced in the form -------------------------------


CRON_PRESETS = [
    ("daily_8am",   "Daily at 08:00",        "0 8 * * *"),
    ("weekly_mon",  "Weekly Monday 08:00",   "0 8 * * mon"),
    ("monthly_1st", "Monthly 1st at 08:00",  "0 8 1 * *"),
]


def _preset_for(cron: str) -> str:
    for key, _label, expr in CRON_PRESETS:
        if expr == cron:
            return key
    return "custom"


# ---------- validation ------------------------------------------------------


def _parse_recipients(raw: str | None) -> list[str]:
    """Recipients come from a textarea (one per line) or a comma list.
    Lower-case, dedupe, drop blanks. Validation happens in
    :func:`_validate_payload` so the form can re-render a friendly
    error rather than this raising."""
    if not raw:
        return []
    parts = re.split(r"[,;\n]", raw)
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        addr = p.strip().lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        out.append(addr)
    return out


def _min_interval_minutes(cron: str) -> float:
    """Smallest gap (minutes) between consecutive firings of ``cron``.

    Sample 30 consecutive firings starting from a stable anchor and
    take the smallest delta. Catches both naive over-firing
    (``* * * * *``, ``*/5 * * * *``) and structured every-N-minute
    lists (``0,5,10,15,20,25,30,35,40,45,50,55 * * * *``).

    Returns ``float('inf')`` for crons that fire less than 30 times in
    the next year — fine, those can't be too frequent.
    """
    trigger = CronTrigger.from_crontab(cron)
    fires: list[datetime] = []
    anchor = datetime(2026, 1, 1, 0, 0, 0)
    last: datetime | None = None
    for _ in range(30):
        nxt = trigger.get_next_fire_time(last, anchor if last is None else last)
        if nxt is None:
            break
        # CronTrigger returns timezone-aware datetimes; strip tz for the
        # delta math since we only care about elapsed time.
        nxt_naive = nxt.replace(tzinfo=None)
        fires.append(nxt_naive)
        last = nxt
    if len(fires) < 2:
        return float("inf")
    deltas = [
        (fires[i + 1] - fires[i]).total_seconds() / 60
        for i in range(len(fires) - 1)
    ]
    return min(deltas)


def _validate_payload(
    title: str,
    prompt: str,
    cron: str,
    recipients: list[str],
) -> str | None:
    """Returns an error string for the first problem, or None if all ok."""
    if not title or not title.strip():
        return "Title is required."
    if len(title) > 255:
        return "Title is too long (max 255 chars)."
    if not prompt or not prompt.strip():
        return "Prompt is required."
    if not cron or not cron.strip():
        return "Cron expression is required."
    try:
        smallest = _min_interval_minutes(cron.strip())
    except Exception as e:
        return f"Cron expression is invalid: {e}"
    if smallest < MIN_INTERVAL_MINUTES:
        return (
            f"Cron fires every {smallest:g} min — minimum interval is "
            f"{MIN_INTERVAL_MINUTES} min."
        )
    if not recipients:
        return "At least one recipient email is required."
    if len(recipients) > MAX_RECIPIENTS:
        return (
            f"Too many recipients ({len(recipients)}). "
            f"Maximum is {MAX_RECIPIENTS}."
        )
    for addr in recipients:
        if not EMAIL_RE.match(addr):
            return f"Not a valid email address: {addr}"
    return None


# ---------- helpers ---------------------------------------------------------


def _get_schedule_or_404(
    db: Session, user: User, schedule_id: int,
) -> ChatSchedule:
    s = db.get(ChatSchedule, schedule_id)
    if not s or s.user_id != user.id:
        raise HTTPException(404, "schedule not found")
    return s


def _annotate_recipients(
    db: Session, recipients: list[str],
) -> list[dict]:
    """Per spec §"form": annotate each recipient with whether it
    matches a known user (= "can reply") or not (= "send-only")."""
    if not recipients:
        return []
    known = {
        u.email.lower()
        for u in db.query(User.email).filter(User.is_active.is_(True)).all()
        if u.email
    }
    return [
        {"email": addr, "can_reply": addr.lower() in known}
        for addr in recipients
    ]


def _follow_up_count_7d(db: Session, schedule: ChatSchedule) -> int:
    """Count fork sessions whose ``forked_from_id`` points at any
    ``ChatSession`` for this schedule, created in the last 7 days.
    Per spec §"index": 'follow-up replies this week'."""
    from datetime import timedelta
    since = datetime.utcnow() - timedelta(days=7)
    parent_ids = [
        sid for (sid,) in
        db.query(ChatSession.id)
        .filter(ChatSession.scheduled_id == schedule.id)
        .all()
    ]
    if not parent_ids:
        return 0
    return (
        db.query(ChatSession.id)
        .filter(
            ChatSession.forked_from_id.in_(parent_ids),
            ChatSession.created_at >= since,
        )
        .count()
    )


# ---------- HTML pages ------------------------------------------------------


@router.get("/chat/schedules", response_class=HTMLResponse)
def schedules_index(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (
        db.query(ChatSchedule)
        .filter(ChatSchedule.user_id == user.id)
        .order_by(ChatSchedule.enabled.desc(), ChatSchedule.updated_at.desc())
        .all()
    )
    decorated = []
    for r in rows:
        decorated.append({
            "row": r,
            "recipients": _annotate_recipients(db, r.recipient_emails or []),
            "follow_ups_7d": _follow_up_count_7d(db, r),
            "preset": _preset_for(r.cron or ""),
        })
    return templates.TemplateResponse(request, "chat_schedules_index.html", {
        "user": user,
        "schedules": decorated,
        "min_interval_minutes": MIN_INTERVAL_MINUTES,
        "max_recipients": MAX_RECIPIENTS,
    })


@router.get("/chat/schedules/new", response_class=HTMLResponse)
def schedules_new(
    request: Request,
    user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(request, "chat_schedule_form.html", {
        "user": user,
        "schedule": None,
        "form": {
            "title": "",
            "prompt": (
                "Summarise what's new with our top competitors since "
                "{{since}}. Lead with anything material (pricing, leadership, "
                "funding, ATS migrations). Cite finding ids. Skip categories "
                "with no new activity."
            ),
            "preset": "weekly_mon",
            "cron": "0 8 * * mon",
            "recipients_text": user.email,
            "enabled": True,
        },
        "presets": CRON_PRESETS,
        "max_recipients": MAX_RECIPIENTS,
        "min_interval_minutes": MIN_INTERVAL_MINUTES,
        "error": None,
    })


@router.get("/chat/schedules/{schedule_id}/edit", response_class=HTMLResponse)
def schedules_edit(
    schedule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = _get_schedule_or_404(db, user, schedule_id)
    return templates.TemplateResponse(request, "chat_schedule_form.html", {
        "user": user,
        "schedule": s,
        "form": {
            "title": s.title,
            "prompt": s.prompt,
            "preset": _preset_for(s.cron or ""),
            "cron": s.cron,
            "recipients_text": "\n".join(s.recipient_emails or []),
            "enabled": s.enabled,
        },
        "presets": CRON_PRESETS,
        "max_recipients": MAX_RECIPIENTS,
        "min_interval_minutes": MIN_INTERVAL_MINUTES,
        "error": None,
    })


# ---------- form submissions -----------------------------------------------


def _resolve_cron(preset: str, custom: str) -> str:
    if preset and preset != "custom":
        for key, _label, expr in CRON_PRESETS:
            if key == preset:
                return expr
    return (custom or "").strip()


@router.post("/chat/schedules", response_class=HTMLResponse)
def schedules_create(
    request: Request,
    title: str = Form(...),
    prompt: str = Form(...),
    preset: str = Form("custom"),
    cron: str = Form(""),
    recipients: str = Form(""),
    enabled: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cron_value = _resolve_cron(preset, cron)
    recipients_list = _parse_recipients(recipients)
    err = _validate_payload(title, prompt, cron_value, recipients_list)
    if err:
        return templates.TemplateResponse(request, "chat_schedule_form.html", {
            "user": user,
            "schedule": None,
            "form": {
                "title": title,
                "prompt": prompt,
                "preset": preset,
                "cron": cron_value,
                "recipients_text": recipients,
                "enabled": enabled is not None,
            },
            "presets": CRON_PRESETS,
            "max_recipients": MAX_RECIPIENTS,
            "min_interval_minutes": MIN_INTERVAL_MINUTES,
            "error": err,
        }, status_code=400)

    row = ChatSchedule(
        user_id=user.id,
        title=title.strip()[:255],
        prompt=prompt.strip(),
        cron=cron_value,
        recipient_emails=recipients_list,
        enabled=(enabled is not None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    if row.enabled:
        scheduler_module.register_schedule(row.id)
    return RedirectResponse("/chat/schedules", status_code=303)


@router.post("/chat/schedules/{schedule_id}/edit", response_class=HTMLResponse)
def schedules_update(
    schedule_id: int,
    request: Request,
    title: str = Form(...),
    prompt: str = Form(...),
    preset: str = Form("custom"),
    cron: str = Form(""),
    recipients: str = Form(""),
    enabled: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = _get_schedule_or_404(db, user, schedule_id)
    cron_value = _resolve_cron(preset, cron)
    recipients_list = _parse_recipients(recipients)
    err = _validate_payload(title, prompt, cron_value, recipients_list)
    if err:
        return templates.TemplateResponse(request, "chat_schedule_form.html", {
            "user": user,
            "schedule": s,
            "form": {
                "title": title,
                "prompt": prompt,
                "preset": preset,
                "cron": cron_value,
                "recipients_text": recipients,
                "enabled": enabled is not None,
            },
            "presets": CRON_PRESETS,
            "max_recipients": MAX_RECIPIENTS,
            "min_interval_minutes": MIN_INTERVAL_MINUTES,
            "error": err,
        }, status_code=400)

    s.title = title.strip()[:255]
    s.prompt = prompt.strip()
    s.cron = cron_value
    s.recipient_emails = recipients_list
    s.enabled = (enabled is not None)
    s.updated_at = datetime.utcnow()
    db.commit()
    if s.enabled:
        scheduler_module.register_schedule(s.id)
    else:
        scheduler_module.unregister_schedule(s.id)
    return RedirectResponse("/chat/schedules", status_code=303)


# ---------- inline JSON action endpoints (used by the dashboard JS) ---------


@router.post("/api/chat/schedules/{schedule_id}/run")
def schedules_run_now(
    schedule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Fire the schedule synchronously. Same code path as the cron, so
    ``last_run_at`` advances and the next cron run sees a fresh
    ``{{since}}`` window."""
    s = _get_schedule_or_404(db, user, schedule_id)
    from ..chat.scheduled import run_scheduled_question
    result = run_scheduled_question(s.id)
    return JSONResponse(result)


@router.post("/api/chat/schedules/{schedule_id}/toggle")
def schedules_toggle(
    schedule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = _get_schedule_or_404(db, user, schedule_id)
    s.enabled = not s.enabled
    s.updated_at = datetime.utcnow()
    db.commit()
    if s.enabled:
        scheduler_module.register_schedule(s.id)
    else:
        scheduler_module.unregister_schedule(s.id)
    return {"id": s.id, "enabled": s.enabled}


@router.delete("/api/chat/schedules/{schedule_id}", status_code=204)
def schedules_delete(
    schedule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = _get_schedule_or_404(db, user, schedule_id)
    scheduler_module.unregister_schedule(s.id)
    db.delete(s)
    db.commit()
