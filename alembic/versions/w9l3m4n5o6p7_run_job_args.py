"""runs.job_args

Revision ID: w9l3m4n5o6p7
Revises: v8k2l3m4n5o6
Create Date: 2026-04-26 00:00:00.000000

Adds the JSON args column to runs so trigger endpoints can enqueue a run
with its parameters (e.g. scan freshness `days`) and the drainer can
dispatch it later. Required for the run-queue spec (docs/runs/01-run-queue.md).
"""
from alembic import op
import sqlalchemy as sa


revision = 'w9l3m4n5o6p7'
down_revision = 'v8k2l3m4n5o6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("job_args", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "job_args")
