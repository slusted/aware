"""chat_schedules + chat_sessions.scheduled_id/forked_from_id

Revision ID: y1n5o6p7q8r9
Revises: x0m4n5o6p7q8
Create Date: 2026-04-27 00:00:00.000000

Tables and columns for the recurring chat-question feature
(docs/chat/02-scheduled-questions.md).

- ``chat_schedules`` — one row per saved schedule. Recipients are stored
  as a JSON list on the row (short list, edited as a unit, never queried
  by individual address — reply auth checks against ``users.email``).
- ``chat_sessions.scheduled_id`` — set when a session was kicked off by
  a schedule's cron firing. NULL for normal user-initiated sessions.
- ``chat_sessions.forked_from_id`` — set when a session was forked from
  another (the reply-to-converse flow copies the original session's
  messages into a fresh session owned by the replying user).

Self-healing against partial state, mirroring the pattern from
x0m4n5o6p7q8: drop ``chat_schedules`` if a half-built copy already
exists, and skip the column add when the column is already present so
re-running the migration after an interrupted upgrade is idempotent.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'y1n5o6p7q8r9'
down_revision = 'x0m4n5o6p7q8'
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _existing_columns(table: str) -> set[str]:
    insp = inspect(op.get_bind())
    if table not in set(insp.get_table_names()):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    tables = _existing_tables()

    # Drop a half-built chat_schedules from a prior failed upgrade, then
    # rebuild cleanly. No real data could exist yet — the feature is
    # gated behind this migration completing successfully.
    if "chat_schedules" in tables:
        op.drop_table("chat_schedules")

    op.create_table(
        "chat_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("cron", sa.String(length=64), nullable=False),
        sa.Column("recipient_emails", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        # NB: cross-FK to chat_sessions(id). chat_sessions also gets a
        # scheduled_id FK back to here, so the two tables form a cycle.
        # SQLite tolerates this since FKs aren't enforced unless
        # ``PRAGMA foreign_keys=ON``; SET NULL on both sides means a
        # delete on either side never cascades into the other.
        sa.Column("last_session_id", sa.Integer(),
                  sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("last_recipient_status", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chat_schedules_user_id", "chat_schedules", ["user_id"])
    op.create_index("ix_chat_schedules_enabled", "chat_schedules", ["enabled"])

    # Two new columns on chat_sessions, both nullable so there's no
    # default-backfill question. Use batch_alter_table for SQLite.
    existing_cols = _existing_columns("chat_sessions")
    with op.batch_alter_table("chat_sessions") as batch_op:
        if "scheduled_id" not in existing_cols:
            batch_op.add_column(sa.Column(
                "scheduled_id", sa.Integer(),
                sa.ForeignKey("chat_schedules.id", ondelete="SET NULL"),
                nullable=True,
            ))
        if "forked_from_id" not in existing_cols:
            batch_op.add_column(sa.Column(
                "forked_from_id", sa.Integer(),
                sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
                nullable=True,
            ))

    # Create indexes after the columns exist. Wrap in try/except so a
    # re-run after a partial upgrade doesn't crash on "index already
    # exists" — same pattern as x0m4n5o6p7q8.
    insp = inspect(op.get_bind())
    existing_indexes = {ix["name"] for ix in insp.get_indexes("chat_sessions")}
    if "ix_chat_sessions_scheduled_id" not in existing_indexes:
        op.create_index("ix_chat_sessions_scheduled_id", "chat_sessions", ["scheduled_id"])
    if "ix_chat_sessions_forked_from_id" not in existing_indexes:
        op.create_index("ix_chat_sessions_forked_from_id", "chat_sessions", ["forked_from_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_forked_from_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_scheduled_id", table_name="chat_sessions")
    with op.batch_alter_table("chat_sessions") as batch_op:
        batch_op.drop_column("forked_from_id")
        batch_op.drop_column("scheduled_id")

    op.drop_index("ix_chat_schedules_enabled", table_name="chat_schedules")
    op.drop_index("ix_chat_schedules_user_id", table_name="chat_schedules")
    op.drop_table("chat_schedules")
