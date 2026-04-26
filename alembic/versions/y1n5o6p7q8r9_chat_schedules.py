"""chat_schedules + chat_sessions.scheduled_id/forked_from_id

Revision ID: y1n5o6p7q8r9
Revises: x0m4n5o6p7q8
Create Date: 2026-04-27 00:00:00.000000

Tables and columns for the recurring chat-question feature
(docs/chat/02-scheduled-questions.md).

- ``chat_schedules`` — one row per saved schedule. Recipients are
  stored as a JSON list on the row.
- ``chat_sessions.scheduled_id`` — set when a session was kicked off
  by a schedule's cron firing.
- ``chat_sessions.forked_from_id`` — set when a session was forked
  from another (the reply-to-converse flow).

History note (load-bearing). The first cut used
``op.batch_alter_table("chat_sessions")`` to add the two new
columns. On Railway with 5776 rows already in chat_messages, batch
mode rebuilds chat_sessions in place and the rebuild silently hung
without raising — SQLite DDL is non-transactional, so partial state
stayed on the volume and the container hot-looped through the same
hang on every boot.

The fix here: use SQLite's native ``ALTER TABLE ADD COLUMN`` (which
does not rebuild the table) for both new columns. Modern SQLite
(>= 3.6) supports ``REFERENCES`` clauses on added columns; the FK
isn't enforced retroactively against existing rows, which is fine —
both columns are nullable and existing rows have no schedule or
fork to point at.

The migration is fully idempotent against the partial state the
crash-loop left behind: if ``chat_schedules`` already exists we
recreate it cleanly (no rows could have landed — the feature was
wedged), and each ALTER + CREATE INDEX is gated on a "does this
exist already?" check.
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


def _existing_indexes(table: str) -> set[str]:
    insp = inspect(op.get_bind())
    if table not in set(insp.get_table_names()):
        return set()
    return {ix["name"] for ix in insp.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # --- chat_schedules: drop any half-built copy and rebuild cleanly. ----
    # Safe to drop because the feature was gated behind this migration —
    # no real schedules could have been created before it succeeded.
    tables = _existing_tables()
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
        # Cross-FK to chat_sessions(id). chat_sessions also gets a
        # scheduled_id FK back to here, so the two tables form a cycle.
        # SQLite tolerates this (FKs aren't enforced unless
        # ``PRAGMA foreign_keys=ON``); SET NULL on both sides means a
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

    # --- chat_schedules indexes (idempotent guards). ----
    sched_idx = _existing_indexes("chat_schedules")
    if "ix_chat_schedules_user_id" not in sched_idx:
        op.create_index("ix_chat_schedules_user_id", "chat_schedules", ["user_id"])
    if "ix_chat_schedules_enabled" not in sched_idx:
        op.create_index("ix_chat_schedules_enabled", "chat_schedules", ["enabled"])

    # --- chat_sessions: add two nullable columns. ----
    # Native ALTER TABLE ADD COLUMN on SQLite (no rebuild). Both
    # columns are nullable so there's no default-backfill question.
    # Modern SQLite supports a REFERENCES clause on the added column;
    # the FK isn't enforced against existing rows, which is fine —
    # those rows have no schedule or fork to point at.
    existing_cols = _existing_columns("chat_sessions")
    if "scheduled_id" not in existing_cols:
        if is_sqlite:
            op.execute(
                "ALTER TABLE chat_sessions ADD COLUMN scheduled_id INTEGER "
                "REFERENCES chat_schedules(id) ON DELETE SET NULL"
            )
        else:
            op.add_column("chat_sessions", sa.Column(
                "scheduled_id", sa.Integer(),
                sa.ForeignKey("chat_schedules.id", ondelete="SET NULL"),
                nullable=True,
            ))

    if "forked_from_id" not in existing_cols:
        if is_sqlite:
            op.execute(
                "ALTER TABLE chat_sessions ADD COLUMN forked_from_id INTEGER "
                "REFERENCES chat_sessions(id) ON DELETE SET NULL"
            )
        else:
            op.add_column("chat_sessions", sa.Column(
                "forked_from_id", sa.Integer(),
                sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
                nullable=True,
            ))

    # --- chat_sessions indexes for the new columns. ----
    sess_idx = _existing_indexes("chat_sessions")
    if "ix_chat_sessions_scheduled_id" not in sess_idx:
        op.create_index("ix_chat_sessions_scheduled_id", "chat_sessions", ["scheduled_id"])
    if "ix_chat_sessions_forked_from_id" not in sess_idx:
        op.create_index("ix_chat_sessions_forked_from_id", "chat_sessions", ["forked_from_id"])


def downgrade() -> None:
    # SQLite ALTER TABLE doesn't support DROP COLUMN before 3.35.
    # Use batch mode here — downgrade isn't on the hot path so the
    # rebuild risk is acceptable, and the safety net is just to leave
    # the columns in place if it fails.
    op.drop_index("ix_chat_sessions_forked_from_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_scheduled_id", table_name="chat_sessions")
    with op.batch_alter_table("chat_sessions") as batch_op:
        batch_op.drop_column("forked_from_id")
        batch_op.drop_column("scheduled_id")

    op.drop_index("ix_chat_schedules_enabled", table_name="chat_schedules")
    op.drop_index("ix_chat_schedules_user_id", table_name="chat_schedules")
    op.drop_table("chat_schedules")
