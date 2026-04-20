"""Add momentum tracking: competitor identifiers + daily metric time-series.

Revision ID: d8f4c19b7a21
Revises: c7f2e9a41a3b
Create Date: 2026-04-18 00:00:00.000000

- Adds app_store_id / play_package / trends_keyword to competitors (all nullable)
- Adds competitor_metrics table for daily momentum signals

All additions are nullable or new tables, so existing rows continue to load.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd8f4c19b7a21'
down_revision = 'c7f2e9a41a3b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE competitors ADD COLUMN app_store_id VARCHAR(32)")
    op.execute("ALTER TABLE competitors ADD COLUMN play_package VARCHAR(128)")
    op.execute("ALTER TABLE competitors ADD COLUMN trends_keyword VARCHAR(128)")

    op.create_table(
        "competitor_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competitor_id", sa.Integer(), sa.ForeignKey("competitors.id"), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("collected_date", sa.String(length=10), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_competitor_metrics_competitor_id", "competitor_metrics", ["competitor_id"])
    op.create_index("ix_competitor_metrics_metric", "competitor_metrics", ["metric"])
    op.create_index("ix_competitor_metrics_collected_date", "competitor_metrics", ["collected_date"])
    op.create_index("ix_competitor_metrics_collected_at", "competitor_metrics", ["collected_at"])
    # Enforce one row per (competitor, metric, day). Upsert at write time.
    op.create_index(
        "ux_competitor_metrics_day",
        "competitor_metrics",
        ["competitor_id", "metric", "collected_date"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_competitor_metrics_day", table_name="competitor_metrics")
    op.drop_index("ix_competitor_metrics_collected_at", table_name="competitor_metrics")
    op.drop_index("ix_competitor_metrics_collected_date", table_name="competitor_metrics")
    op.drop_index("ix_competitor_metrics_metric", table_name="competitor_metrics")
    op.drop_index("ix_competitor_metrics_competitor_id", table_name="competitor_metrics")
    op.drop_table("competitor_metrics")
    with op.batch_alter_table("competitors") as batch:
        batch.drop_column("trends_keyword")
        batch.drop_column("play_package")
        batch.drop_column("app_store_id")
