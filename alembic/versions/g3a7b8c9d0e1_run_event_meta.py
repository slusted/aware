"""Add meta JSON column to run_events.

Revision ID: g3a7b8c9d0e1
Revises: f2c8d4e59a13
Create Date: 2026-04-19 12:30:00.000000

Lets a RunEvent carry structured context (e.g. a material finding's
competitor_id / signal_type / title / url) so the live-log UI can render
it as a badge + clickable link instead of plain text.
"""
from alembic import op
import sqlalchemy as sa


revision = 'g3a7b8c9d0e1'
down_revision = 'f2c8d4e59a13'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("run_events") as batch:
        batch.add_column(sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("run_events") as batch:
        batch.drop_column("meta")
