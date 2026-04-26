"""Discovery helper for chat job-completion notifications.

When a long-running job (deep research, eventually market synthesis /
digest) finishes, we want the chat that started it to learn about it
without the user having to ask. The chat page polls
``GET /api/chat/{id}/notifications`` every 15s; that endpoint calls
``discover_completed_research`` here.

Idempotency: each call inserts a ``ChatMessage`` row with ``role=
'notification'`` per newly-completed report, then commits. A second
call after the commit returns ``[]`` because the existence check (by
``deep_research_report.id`` stored in ``tool_payload``) excludes
already-notified reports. Two tabs polling concurrently is a small
race window — SQLite serialises writes, but if a duplicate slips
through it's one extra bubble in the thread, not a correctness bug.

v1 scope: DeepResearchReport only. Market synthesis / digest follow
the same pattern in later specs — extend the helper, don't fork it.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import ChatMessage, Competitor, DeepResearchReport, Run


_TERMINAL_RESEARCH_STATUSES = ("ready", "failed")


def _already_notified_research_ids(db: Session, session_id: int) -> set[int]:
    """Return the set of deep_research_report.id values that already
    have a notification row in this session. Cheap: indexed lookup on
    chat_messages(session_id) plus role filter."""
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "notification",
        )
        .all()
    )
    out: set[int] = set()
    for r in rows:
        pl = r.tool_payload or {}
        if pl.get("kind") != "deep_research_report":
            continue
        rid = pl.get("id")
        if isinstance(rid, int):
            out.add(rid)
    return out


def discover_completed_research(
    db: Session, session_id: int
) -> list[ChatMessage]:
    """Find DeepResearchReports triggered from this chat session that
    have reached a terminal state and don't yet have a notification
    message. Insert one ChatMessage(role='notification') per match,
    commit, and return the new rows in id order.

    Returns an empty list when nothing is new — the caller turns that
    into a 204 No Content for HTMX (which then no-ops the swap).
    """
    triggered_tag = f"chat:{session_id}"

    candidates: list[DeepResearchReport] = (
        db.query(DeepResearchReport)
        .join(Run, Run.id == DeepResearchReport.run_id)
        .filter(
            Run.triggered_by == triggered_tag,
            DeepResearchReport.status.in_(_TERMINAL_RESEARCH_STATUSES),
        )
        .order_by(DeepResearchReport.id.asc())
        .all()
    )
    if not candidates:
        return []

    already = _already_notified_research_ids(db, session_id)
    fresh = [r for r in candidates if r.id not in already]
    if not fresh:
        return []

    competitor_ids = {r.competitor_id for r in fresh}
    competitors_by_id: dict[int, Competitor] = {
        c.id: c
        for c in db.query(Competitor).filter(Competitor.id.in_(competitor_ids)).all()
    }

    new_rows: list[ChatMessage] = []
    for report in fresh:
        comp = competitors_by_id.get(report.competitor_id)
        comp_name = comp.name if comp else f"competitor #{report.competitor_id}"
        url = f"/competitors/{report.competitor_id}#research"
        finished = report.finished_at or report.started_at
        payload = {
            "kind": "deep_research_report",
            "id": report.id,
            "competitor_id": report.competitor_id,
            "competitor_name": comp_name,
            "status": report.status,
            "url": url,
            "finished_at": finished.isoformat() if finished else None,
            "error": (report.error or "")[:200] if report.status == "failed" else None,
        }
        row = ChatMessage(
            session_id=session_id,
            role="notification",
            content="",
            tool_payload=payload,
        )
        db.add(row)
        new_rows.append(row)

    db.commit()
    for row in new_rows:
        db.refresh(row)
    return new_rows


__all__ = ["discover_completed_research"]
