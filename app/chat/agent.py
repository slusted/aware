"""Chat agent — turn loop wrapping the Anthropic Messages API.

A turn is one user submission. Within a turn the model may call tools
multiple times before producing its final text. Tools fall into two
buckets:

- Read tools execute inline; results feed straight back to the model.
- Write tools (``requires_confirmation=True``) pause the turn: the
  ``tool_use`` row is persisted with ``confirmation_status="pending"``,
  the SSE stream emits a ``confirmation_pending`` event, and the stream
  ends. The browser renders Confirm/Cancel cards; the user's choice
  posts to ``/api/chat/{id}/resume`` which re-enters this module to
  execute the confirmed calls and continue the model loop.

State is reconstructed from ``ChatMessage`` rows on every entry so the
turn is resumable across reloads/restarts — there's no in-memory
session state outside the SDK call.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Iterator

import anthropic
from sqlalchemy.orm import Session

from .. import pricing, skills as skills_module
from ..models import ChatMessage, ChatSession, User
from ..usage import record_claude
from . import tools as tools_module


# Per-turn safety knobs. Override via env vars when tuning in prod.
MAX_TOOL_CALLS_PER_TURN = int(os.environ.get("CHAT_MAX_TOOL_CALLS", "8"))
TURN_TIMEOUT_S = int(os.environ.get("CHAT_TURN_TIMEOUT_S", "90"))
SESSION_COST_HARD_USD = float(os.environ.get("CHAT_SESSION_COST_HARD_USD", "5.0"))
SESSION_COST_WARN_USD = float(os.environ.get("CHAT_SESSION_COST_WARN_USD", "1.0"))
MAX_TOKENS_PER_TURN = int(os.environ.get("CHAT_MAX_TOKENS_PER_TURN", "4096"))


_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is not None:
        return _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    _client = anthropic.Anthropic()
    return _client


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ----- helpers ---------------------------------------------------------------


def _sse(event: str, data: dict) -> str:
    """Render one SSE event. Each line is `event:` then `data:` then a blank
    line. Data is JSON so the browser can parse without extra ceremony."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _load_config() -> dict:
    path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _render_system_prompt(user: User) -> tuple[str, list[tools_module.Tool]]:
    template = skills_module.load_active("chat_system") or ""
    cfg = _load_config()
    tools = tools_module.tools_for_role(user.role)
    catalog = tools_module.render_catalog_for_prompt(tools)
    body = (
        template
        .replace("{{our_company}}", cfg.get("company", "our company"))
        .replace("{{our_industry}}", cfg.get("industry", "our industry"))
        .replace("{{tool_catalog}}", catalog)
    )
    return body, tools


def _rebuild_messages(db: Session, session_id: int) -> list[dict]:
    """Re-render all DB rows into the Anthropic Messages API format.

    Each LLM "turn" in the DB is a cluster of rows:
      assistant text → tool_use rows → tool_result rows
    where the read-tool inline-result rows MAY appear interleaved with
    sibling tool_use rows when the agent wrote them in the order they
    were dispatched. The Anthropic API requires a stricter shape: the
    assistant message owns ALL its tool_use blocks together, and the
    next user message owns ALL the matching tool_result blocks.

    We rebuild defensively: don't flush the in-flight assistant when we
    see a tool_result row — only flush on the next assistant/user text
    boundary. That way any persisted interleaving heals on the way out
    to the API. Also tolerates a tool_result with no preceding tool_use
    by dropping it (avoids confusing the API with an orphan).
    """
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id.asc())
        .all()
    )
    messages: list[dict] = []
    pending_assistant: dict | None = None
    pending_tool_results: list[dict] = []
    pending_tool_use_ids: set[str] = set()

    def _flush_turn():
        nonlocal pending_assistant, pending_tool_results, pending_tool_use_ids
        if pending_assistant is not None and pending_assistant["content"]:
            messages.append(pending_assistant)
        if pending_tool_results:
            messages.append({"role": "user", "content": pending_tool_results})
        pending_assistant = None
        pending_tool_results = []
        pending_tool_use_ids = set()

    for row in rows:
        if row.role == "user":
            _flush_turn()
            messages.append({"role": "user", "content": [{"type": "text", "text": row.content}]})
        elif row.role == "assistant":
            _flush_turn()
            content: list[dict] = []
            if row.content:
                content.append({"type": "text", "text": row.content})
            pending_assistant = {"role": "assistant", "content": content}
        elif row.role == "tool_use":
            if pending_assistant is None:
                pending_assistant = {"role": "assistant", "content": []}
            pl = row.tool_payload or {}
            tu_id = pl.get("id")
            if tu_id:
                pending_tool_use_ids.add(tu_id)
            pending_assistant["content"].append({
                "type": "tool_use",
                "id": tu_id,
                "name": pl.get("name"),
                "input": pl.get("input") or {},
            })
        elif row.role == "tool_result":
            pl = row.tool_payload or {}
            tool_use_id = pl.get("tool_use_id")
            # An orphan tool_result with no matching tool_use in this
            # turn would corrupt the API call — drop it. (Belt-and-
            # braces against legacy rows or hand-edited DBs.)
            if not tool_use_id or tool_use_id not in pending_tool_use_ids:
                continue
            output = pl.get("output")
            text = output if isinstance(output, str) else json.dumps(output, default=str)
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": text,
                "is_error": bool(pl.get("is_error")),
            })
        # error rows are display-only; skip them when rebuilding for the API

    _flush_turn()

    # Drop any trailing assistant whose tool_use blocks lack matching
    # tool_results — sending it would crash the next API call. The
    # caller will re-emit those tool_uses in the next iteration.
    while messages and messages[-1].get("role") == "assistant":
        last = messages[-1]
        unresolved = any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in last.get("content", [])
        )
        if not unresolved:
            break
        # Look at trailing user-tool_result message (if any) to see
        # whether it pairs every tool_use; if not, surface the gap by
        # removing the assistant. With the current persistence order
        # this should never trigger — we only call _rebuild_messages
        # right before an API call when all results are present.
        # Defensive: if it does fire, the next loop iteration will
        # resurface the missing tool_use as a fresh model response.
        messages.pop()
        break

    return messages


def _persist_message(
    db: Session,
    session_id: int,
    *,
    role: str,
    content: str = "",
    tool_payload: dict | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cost_usd: float | None = None,
    stop_reason: str | None = None,
) -> ChatMessage:
    row = ChatMessage(
        session_id=session_id,
        role=role,
        content=content,
        tool_payload=tool_payload or {},
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
        stop_reason=stop_reason,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _bump_session_totals(
    db: Session,
    session: ChatSession,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    cost_usd: float,
):
    session.total_input_tokens = (session.total_input_tokens or 0) + input_tokens
    session.total_output_tokens = (session.total_output_tokens or 0) + output_tokens
    session.total_cache_read_tokens = (session.total_cache_read_tokens or 0) + cache_read_tokens
    session.total_cache_write_tokens = (session.total_cache_write_tokens or 0) + cache_write_tokens
    session.total_cost_usd = (session.total_cost_usd or 0.0) + cost_usd
    session.updated_at = datetime.utcnow()
    db.commit()


def _maybe_set_title(db: Session, session: ChatSession, first_user_text: str):
    if session.title and session.title != "New chat":
        return
    snippet = (first_user_text or "").strip().splitlines()[0] if first_user_text else ""
    if not snippet:
        return
    if len(snippet) > 80:
        snippet = snippet[:79].rstrip() + "…"
    session.title = snippet
    db.commit()


# ----- pending-tool helpers --------------------------------------------------


def _pending_tool_uses(db: Session, session_id: int) -> list[ChatMessage]:
    """Tool_use rows still awaiting user confirmation. Ordered oldest first."""
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "tool_use",
        )
        .order_by(ChatMessage.id.asc())
        .all()
    )
    out = []
    for r in rows:
        pl = r.tool_payload or {}
        if pl.get("requires_confirmation") and pl.get("confirmation_status") == "pending":
            out.append(r)
    return out


def _has_tool_result(db: Session, session_id: int, tool_use_id: str) -> bool:
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "tool_result",
        )
        .all()
    )
    for r in rows:
        if (r.tool_payload or {}).get("tool_use_id") == tool_use_id:
            return True
    return False


# ----- the turn loop ---------------------------------------------------------


def run_turn(
    db: Session,
    session: ChatSession,
    user: User,
    *,
    user_text: str | None = None,
) -> Iterator[str]:
    """Main entry point. Streams SSE strings.

    - When ``user_text`` is given we persist a new user message and start
      a fresh model call.
    - When ``user_text`` is None we resume an existing turn — execute any
      tool_uses whose confirmations have been resolved (confirmed → run,
      cancelled → tool_result with the cancellation), then continue the
      model loop.
    """
    client = get_client()
    if client is None:
        yield _sse("error", {"message": "ANTHROPIC_API_KEY is not set. Add it on /settings/keys."})
        return

    if (session.total_cost_usd or 0.0) >= SESSION_COST_HARD_USD:
        yield _sse("error", {
            "message": f"Session has hit the hard cost cap (${SESSION_COST_HARD_USD:.2f}). "
                       "Start a new chat to continue.",
        })
        return

    if user_text is not None:
        text = user_text.strip()
        if text:
            _persist_message(db, session.id, role="user", content=text)
            _maybe_set_title(db, session, text)

    yield _sse("turn_start", {"session_id": session.id})

    # Resume path: execute confirmed-but-unrun tool_uses and emit results
    # before the next model call.
    pending = _pending_tool_uses(db, session.id)
    for row in pending:
        pl = row.tool_payload or {}
        decision = pl.get("confirmation_status")
        if decision == "pending":
            # Still awaiting the user — bail; the UI will keep showing the card.
            yield _sse("waiting_for_confirmation", {"tool_use_id": pl.get("id")})
            return
        if _has_tool_result(db, session.id, pl.get("id")):
            continue
        if decision == "cancelled":
            result_payload = {
                "tool_use_id": pl.get("id"),
                "output": {"cancelled": True, "message": "User cancelled this action."},
                "is_error": False,
            }
            _persist_message(db, session.id, role="tool_result", tool_payload=result_payload)
            yield _sse("tool_result", {
                "tool_use_id": pl.get("id"),
                "name": pl.get("name"),
                "output": result_payload["output"],
                "is_error": False,
            })
            continue
        if decision == "confirmed":
            yield _sse("tool_running", {"tool_use_id": pl.get("id"), "name": pl.get("name")})
            output, is_error = tools_module.execute_tool(
                pl.get("name"), pl.get("input") or {}, db, user
            )
            result_payload = {
                "tool_use_id": pl.get("id"),
                "output": output,
                "is_error": is_error,
            }
            _persist_message(db, session.id, role="tool_result", tool_payload=result_payload)
            yield _sse("tool_result", {
                "tool_use_id": pl.get("id"),
                "name": pl.get("name"),
                "output": output,
                "is_error": is_error,
            })

    system_text, tools = _render_system_prompt(user)
    anthropic_tools = tools_module.to_anthropic_schema(tools)

    deadline = time.monotonic() + TURN_TIMEOUT_S
    tool_calls_this_turn = 0
    model = session.model or "claude-sonnet-4-6"

    while True:
        if time.monotonic() > deadline:
            _persist_message(db, session.id, role="error",
                             content=f"Turn exceeded {TURN_TIMEOUT_S}s timeout.")
            yield _sse("error", {"message": f"Turn timed out after {TURN_TIMEOUT_S}s."})
            return

        messages = _rebuild_messages(db, session.id)
        if not messages:
            yield _sse("error", {"message": "No messages to respond to."})
            return

        try:
            with client.messages.stream(
                model=model,
                max_tokens=MAX_TOKENS_PER_TURN,
                system=[{
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=anthropic_tools,
                messages=messages,
            ) as stream:
                for chunk in stream.text_stream:
                    if chunk:
                        yield _sse("text_delta", {"text": chunk})
                final = stream.get_final_message()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _persist_message(db, session.id, role="error", content=err)
            yield _sse("error", {"message": err})
            return

        # Per-turn cost ledger.
        usage = getattr(final, "usage", None)
        it = int(getattr(usage, "input_tokens", 0) or 0)
        ot = int(getattr(usage, "output_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cost = pricing.claude_cost(model, it, ot, cr, cw)

        # Independent usage_events row so chat shows up in the global usage page.
        try:
            record_claude(model=model, usage=usage, operation="chat.turn")
        except Exception:
            pass

        # Pull text + tool_use blocks out of the final message.
        assistant_text_parts: list[str] = []
        tool_use_blocks: list[dict] = []
        for block in (final.content or []):
            btype = getattr(block, "type", None)
            if btype == "text":
                assistant_text_parts.append(getattr(block, "text", "") or "")
            elif btype == "tool_use":
                tool_use_blocks.append({
                    "id": getattr(block, "id", None),
                    "name": getattr(block, "name", None),
                    "input": getattr(block, "input", None) or {},
                })
        assistant_text = "".join(assistant_text_parts)
        stop_reason = getattr(final, "stop_reason", None)

        _persist_message(
            db, session.id,
            role="assistant",
            content=assistant_text,
            input_tokens=it,
            output_tokens=ot,
            cache_read_tokens=cr,
            cache_write_tokens=cw,
            cost_usd=cost,
            stop_reason=stop_reason,
        )
        _bump_session_totals(
            db, session,
            input_tokens=it, output_tokens=ot,
            cache_read_tokens=cr, cache_write_tokens=cw,
            cost_usd=cost,
        )

        yield _sse("usage", {
            "input_tokens": it, "output_tokens": ot,
            "cache_read_tokens": cr, "cache_write_tokens": cw,
            "cost_usd": cost,
            "session_total_cost_usd": session.total_cost_usd,
            "stop_reason": stop_reason,
        })

        if (session.total_cost_usd or 0.0) >= SESSION_COST_WARN_USD < SESSION_COST_HARD_USD:
            yield _sse("cost_warning", {
                "session_total_cost_usd": session.total_cost_usd,
                "warn_at": SESSION_COST_WARN_USD,
                "hard_at": SESSION_COST_HARD_USD,
            })

        if stop_reason != "tool_use" or not tool_use_blocks:
            yield _sse("turn_end", {
                "stop_reason": stop_reason,
                "session_total_cost_usd": session.total_cost_usd,
            })
            return

        # Two-phase dispatch so DB row order matches the Anthropic message
        # shape (assistant → tool_use* → tool_result*). Persisting a read
        # tool's result inline between sibling tool_use blocks corrupts the
        # rebuild — the model rejects "messages.N: tool_use ids were found
        # without tool_result blocks immediately after" because we end up
        # splitting one assistant turn into two.
        #
        # Phase 1: persist every tool_use row from this LLM response.
        # Phase 2: run reads (and persist their tool_results) or mark
        # writes pending. By the time Phase 2 runs, all sibling tool_use
        # rows are already on disk, so each tool_result's row index is
        # strictly greater than every tool_use it could belong to.
        any_pending = False
        prepared: list[tuple[dict, "tools_module.Tool | None", bool]] = []
        for block in tool_use_blocks:
            tool_calls_this_turn += 1
            if tool_calls_this_turn > MAX_TOOL_CALLS_PER_TURN:
                msg = (
                    f"Hit the per-turn tool-call cap ({MAX_TOOL_CALLS_PER_TURN}). "
                    "Tell me how you'd like to continue."
                )
                _persist_message(db, session.id, role="error", content=msg)
                yield _sse("error", {"message": msg})
                return

            tool_def = tools_module.get_tool(block["name"])
            requires_confirmation = bool(tool_def and tool_def.requires_confirmation)
            payload = {
                "id": block["id"],
                "name": block["name"],
                "input": block["input"],
                "requires_confirmation": requires_confirmation,
                "confirmation_status": "pending" if requires_confirmation else "auto",
            }
            if requires_confirmation and tool_def and tool_def.confirmation_summary:
                try:
                    payload["confirmation_summary"] = tool_def.confirmation_summary(block["input"])
                except Exception:
                    payload["confirmation_summary"] = f"Run {block['name']}?"

            _persist_message(db, session.id, role="tool_use", tool_payload=payload)
            yield _sse("tool_use", payload)
            prepared.append((block, tool_def, requires_confirmation))

        for block, _tool_def, requires_confirmation in prepared:
            if requires_confirmation:
                any_pending = True
                continue

            yield _sse("tool_running", {"tool_use_id": block["id"], "name": block["name"]})
            output, is_error = tools_module.execute_tool(
                block["name"], block["input"], db, user
            )
            result_payload = {
                "tool_use_id": block["id"],
                "output": output,
                "is_error": is_error,
            }
            _persist_message(db, session.id, role="tool_result", tool_payload=result_payload)
            yield _sse("tool_result", {
                "tool_use_id": block["id"],
                "name": block["name"],
                "output": output,
                "is_error": is_error,
            })

        if any_pending:
            yield _sse("confirmation_pending", {
                "session_id": session.id,
                "message": "Awaiting your confirmation on the action(s) above.",
            })
            return
        # Otherwise loop and re-call the model with the new tool_results.
