"""Add per-competitor search-quality overrides.

Revision ID: e1f2a3b4c5d6
Revises: d8f4c19b7a21
Create Date: 2026-04-18 00:00:00.000000

- min_relevance_score: override global floor for big-brand competitors with noisy results
- social_score_multiplier: override global social-source boost

Both nullable — NULL = use global env defaults.
"""
from alembic import op
import sqlalchemy as sa


revision = 'e1f2a3b4c5d6'
down_revision = 'd8f4c19b7a21'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("competitors") as batch:
        batch.add_column(sa.Column("min_relevance_score", sa.Float(), nullable=True))
        batch.add_column(sa.Column("social_score_multiplier", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("competitors") as batch:
        batch.drop_column("social_score_multiplier")
        batch.drop_column("min_relevance_score")
