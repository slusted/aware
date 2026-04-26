"""chat_sessions + chat_messages

Revision ID: w9l3m4n5o6p7
Revises: v8k2l3m4n5o6
Create Date: 2026-04-26 00:00:00.000000

Tables for the agentic chat surface (docs/chat/01-chat.md). One row per
session, one row per message; assistant turn loops persist tool_use and
tool_result rows so the conversation re-renders verbatim from the DB.
"""
from alembic import op
import sqlalchemy as sa


revision = 'w9l3m4n5o6p7'
down_revision = 'v8k2l3m4n5o6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
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
                  nullable=False, index=True),
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
