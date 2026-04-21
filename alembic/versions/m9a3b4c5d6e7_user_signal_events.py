"""User signal events — append-only log of per-user interactions with findings.

Revision ID: m9a3b4c5d6e7
Revises: l8f2a3b4c6d7
Create Date: 2026-04-21 00:00:00.000000

First building block of the ranker (spec docs/ranker/01-signal-log.md). This
table is the source of truth for preference learning; the existing
SignalView table is unchanged — it remains the current-state view (pin /
dismiss / snooze) while this log captures every interaction as an
immutable fact. Pin/dismiss/snooze writes will dual-write into both in
the same transaction (wired up in app/routes/findings.py).
"""
from alembic import op
import sqlalchemy as sa


revision = 'm9a3b4c5d6e7'
down_revision = 'l8f2a3b4c6d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_signal_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Nullable for non-finding events (e.g. chat_pref_update). ON DELETE
        # SET NULL so removing a finding doesn't wipe the behavioural history
        # that the rollup depends on.
        sa.Column(
            "finding_id",
            sa.Integer(),
            sa.ForeignKey("findings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        # Raw event-specific magnitude (e.g. dwell_ms). Never a precomputed
        # weight — weight mapping lives in the rollup so it can be retuned
        # without backfilling.
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("ts", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    # Primary access pattern: recent events for one user (rollup + debug UI).
    op.create_index(
        "ix_user_signal_events_user_ts",
        "user_signal_events",
        ["user_id", sa.text("ts DESC")],
    )
    # Rollup reads per-type slices for weight accounting.
    op.create_index(
        "ix_user_signal_events_user_type_ts",
        "user_signal_events",
        ["user_id", "event_type", sa.text("ts DESC")],
    )
    # Reverse lookup ("which users reacted to this finding").
    op.create_index(
        "ix_user_signal_events_finding",
        "user_signal_events",
        ["finding_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_signal_events_finding", table_name="user_signal_events")
    op.drop_index("ix_user_signal_events_user_type_ts", table_name="user_signal_events")
    op.drop_index("ix_user_signal_events_user_ts", table_name="user_signal_events")
    op.drop_table("user_signal_events")
