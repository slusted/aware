"""Add findings.summary — LLM-written clean snippet for the stream card.

Revision ID: i5c9d0e1f2a3
Revises: h4b8c9d0e1f2
Create Date: 2026-04-20 00:00:00.000000

`content` holds raw trafilatura extract which often starts with navigation
markdown ("[Skip to content]…[Startups](…)"). The stream card truncates
that to 280 chars, so the reader sees boilerplate instead of the actual
story. `summary` is a short (≤320 char) LLM-written snippet that the card
shows in place of the raw extract, falling back to `content` for legacy
rows that haven't been backfilled yet.
"""
from alembic import op
import sqlalchemy as sa


revision = 'i5c9d0e1f2a3'
down_revision = 'h4b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE findings ADD COLUMN summary TEXT")


def downgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("summary")
