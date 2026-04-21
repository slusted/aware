"""Positioning snapshots + Competitor.positioning_pages.

Revision ID: p2e6f7a8b9c0
Revises: o1d5e6f7a8b9
Create Date: 2026-04-22 00:00:00.000000

Adds the `positioning_snapshots` table (one row per extraction of a
competitor's marketing-page positioning, append-only) and the
`competitors.positioning_pages` JSON column (optional per-competitor
override list of URLs the extractor should fetch; empty = auto-probe).
"""
from alembic import op
import sqlalchemy as sa


revision = 'p2e6f7a8b9c0'
down_revision = 'o1d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "positioning_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "competitor_id",
            sa.Integer(),
            sa.ForeignKey("competitors.id"),
            nullable=False,
        ),
        sa.Column("pillars", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("body_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_urls", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index(
        "ix_positioning_snapshots_competitor_id",
        "positioning_snapshots",
        ["competitor_id"],
    )
    op.create_index(
        "ix_positioning_snapshots_source_hash",
        "positioning_snapshots",
        ["source_hash"],
    )
    op.create_index(
        "ix_positioning_snapshots_created_at",
        "positioning_snapshots",
        ["created_at"],
    )
    # Covering index used by both the "latest per competitor" and "history"
    # queries. DESC matches the read direction.
    op.create_index(
        "ix_positioning_snapshots_competitor_created",
        "positioning_snapshots",
        ["competitor_id", sa.text("created_at DESC")],
    )

    op.add_column(
        "competitors",
        sa.Column(
            "positioning_pages",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("competitors") as batch:
        batch.drop_column("positioning_pages")
    op.drop_index(
        "ix_positioning_snapshots_competitor_created",
        table_name="positioning_snapshots",
    )
    op.drop_index(
        "ix_positioning_snapshots_created_at",
        table_name="positioning_snapshots",
    )
    op.drop_index(
        "ix_positioning_snapshots_source_hash",
        table_name="positioning_snapshots",
    )
    op.drop_index(
        "ix_positioning_snapshots_competitor_id",
        table_name="positioning_snapshots",
    )
    op.drop_table("positioning_snapshots")
