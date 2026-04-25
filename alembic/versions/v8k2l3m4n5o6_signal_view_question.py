"""signal_views.question

Revision ID: v8k2l3m4n5o6
Revises: u7j1k2l3m4n5
Create Date: 2026-04-25 00:00:00.000000

Adds the per-(user, finding) follow-up question column for spec 09.
The question lives on SignalView so it stays editable and tied to the
user-finding pair; it deliberately does NOT go in the event log
(user_signal_events) since the log is meant to be append-only minimal
events, not user-authored content.
"""
from alembic import op
import sqlalchemy as sa


revision = 'v8k2l3m4n5o6'
down_revision = 'u7j1k2l3m4n5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "signal_views",
        sa.Column("question", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("signal_views", "question")
