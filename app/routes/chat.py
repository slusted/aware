"""HTTP surface for the chat agent.

Endpoints:
- ``POST /api/chat/sessions``       create a new ChatSession
- ``POST /api/chat/{id}/messages``  submit a user message → SSE stream
- ``POST /api/chat/{id}/confirm``   resolve pending tool confirmations → SSE stream
- ``DELETE /api/chat/{id}``         soft-archive (status flip)
- ``POST /api/chat/{id}/rename``    update title

Sessions are user-scoped: any read or write checks ``session.user_id ==
current_user.id`` and 404s otherwise (don't leak existence to other
users).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..chat import agent as chat_agent
from ..deps import get_current_user, get_db
from ..models import ChatMessage, ChatSession, User


router = APIRouter(prefix="/api/chat", tags=["chat"])


def _get_session_or_404(db: Session, user: User, session_id: int) -> ChatSession:
    s = db.get(ChatSession, session_id)
    if not s or s.user_id != user.id:
        raise HTTPException(404, "session not found")
    return s


class SessionCreateIn(BaseModel):
    title: Optional[str] = None
    first_message: Optional[str] = None


@router.post("/sessions", status_code=201)
def create_session(
    payload: SessionCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not chat_agent.has_api_key():
        raise HTTPException(
            400,
            detail="ANTHROPIC_API_KEY is not set. Add it on /settings/keys before starting a chat.",
        )
    session = ChatSession(
        user_id=user.id,
        title=(payload.title or "New chat").strip()[:255] or "New chat",
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    if payload.first_message and payload.first_message.strip():
        msg = ChatMessage(
            session_id=session.id,
            role="user",
            content=payload.first_message.strip(),
        )
        db.add(msg)
        # Title-from-first-line so the sidebar isn't full of "New chat".
        if (session.title or "New chat") == "New chat":
            snippet = payload.first_message.strip().splitlines()[0]
            if len(snippet) > 80:
                snippet = snippet[:79].rstrip() + "…"
            if snippet:
                session.title = snippet
        db.commit()

    return {"id": session.id, "title": session.title}


class MessageIn(BaseModel):
    text: str


@router.post("/{session_id}/messages")
def post_message(
    session_id: int,
    payload: MessageIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = _get_session_or_404(db, user, session_id)
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(400, "message text required")
    if session.status != "active":
        raise HTTPException(409, "session is archived")

    return StreamingResponse(
        chat_agent.run_turn(db, session, user, user_text=text),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class ConfirmDecision(BaseModel):
    tool_use_id: str
    decision: str  # "confirm" | "cancel"


class ConfirmIn(BaseModel):
    confirmations: list[ConfirmDecision]


@router.post("/{session_id}/confirm")
def confirm_and_resume(
    session_id: int,
    payload: ConfirmIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = _get_session_or_404(db, user, session_id)
    if session.status != "active":
        raise HTTPException(409, "session is archived")

    decisions: dict[str, str] = {
        c.tool_use_id: ("confirmed" if c.decision == "confirm" else "cancelled")
        for c in payload.confirmations
    }

    # Apply decisions to the relevant tool_use rows.
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session.id,
            ChatMessage.role == "tool_use",
        )
        .all()
    )
    touched = 0
    for row in rows:
        pl = dict(row.tool_payload or {})
        tool_use_id = pl.get("id")
        if not tool_use_id or tool_use_id not in decisions:
            continue
        if pl.get("confirmation_status") != "pending":
            continue
        pl["confirmation_status"] = decisions[tool_use_id]
        row.tool_payload = pl
        touched += 1
    if touched:
        db.commit()

    return StreamingResponse(
        chat_agent.run_turn(db, session, user, user_text=None),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class RenameIn(BaseModel):
    title: str


@router.post("/{session_id}/rename")
def rename_session(
    session_id: int,
    payload: RenameIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = _get_session_or_404(db, user, session_id)
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    session.title = title[:255]
    db.commit()
    return {"id": session.id, "title": session.title}


@router.delete("/{session_id}", status_code=204)
def archive_session(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = _get_session_or_404(db, user, session_id)
    session.status = "archived"
    db.commit()
