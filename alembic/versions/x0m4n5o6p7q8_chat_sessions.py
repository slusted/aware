"""chat_sessions + chat_messages

Revision ID: x0m4n5o6p7q8
Revises: w9l3m4n5o6p7
Create Date: 2026-04-26 00:00:00.000000

Tables for the agentic chat surface (docs/chat/01-chat.md). One row per
session, one row per message; assistant turn loops persist tool_use and
tool_result rows so the conversation re-renders verbatim from the DB.

Self-healing against partial state. The first published cut of this
migration combined ``Column(..., index=True)`` with explicit
``op.create_index`` calls of the same name (``ix_<table>_<column>``);
SQLAlchemy auto-creates those at table-creation time, so the second
``op.create_index`` raised ``index already exists`` and crashed the
upgrade mid-flight on Railway. SQLite DDL is non-transactional in
alembic, so the partial create_table commits stay on disk and the
container hot-loops on the same crash on every boot.

The fix: drop any pre-existing chat_* table at the start of the
upgrade and rebuild cleanly. Safe because the chat surface was wedged
on the broken migration the entire time, so no chat rows ever
landed in production.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'x0m4n5o6p7q8'
down_revision = 'w9l3m4n5o6p7'
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    # Drop any half-built rows from the broken first cut (see module
    # docstring). These tables were never readable through the app, so
    # nothing of value is being thrown away.
    tables = _existing_tables()
    if "chat_messages" in tables:
        op.drop_table("chat_messages")
    if "chat_sessions" in tables:
        op.drop_table("chat_sessions")

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False, server_default="New chat"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("model", sa.String(length=64), nullable=False, server_default="claude-sonnet-4-6"),
        sa.Column("total_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cost_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chat_sessions_user_id", "chat_sessions", ["user_id"])
    op.create_index("ix_chat_sessions_status", "chat_sessions", ["status"])
    op.create_index("ix_chat_sessions_created_at", "chat_sessions", ["created_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(),
                  sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])
    op.create_index("ix_chat_messages_role", "chat_messages", ["role"])
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"])
    op.create_index("ix_chat_messages_session_id_id", "chat_messages", ["session_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_id_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_created_at", table_name="chat_messages")
    op.drop_index("ix_chat_messages_role", table_name="chat_messages")
    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
    op.drop_table("chat_messages")

    op.drop_index("ix_chat_sessions_created_at", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_status", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_user_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
