"""Add search_provider, score, published_at to findings.

Revision ID: c7f2e9a41a3b
Revises: 5a4568830d8c
Create Date: 2026-04-17 20:05:00.000000

Adds three nullable columns so we can see which search provider delivered a
finding, how confident it scored the relevance, and when the source actually
published the content (vs when we ingested it). All columns are nullable so
pre-existing rows continue to load.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7f2e9a41a3b'
down_revision = '5a4568830d8c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("search_provider", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("score", sa.Float(), nullable=True))
        batch.add_column(sa.Column("published_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_findings_search_provider",
        "findings",
        ["search_provider"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_findings_search_provider", table_name="findings")
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("published_at")
        batch.drop_column("score")
        batch.drop_column("search_provider")
