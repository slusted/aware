"""market_synthesis_reports

Revision ID: t6i0j1k2l3m4
Revises: s5h9i0j1k2l3
Create Date: 2026-04-25 00:00:00.000000

Adds the `market_synthesis_reports` table — one row per cross-competitor
Gemini Deep Research synthesis. Append-only; latest row is the 'current'
synthesis on /market, older rows are history. Unlike deep_research_reports
there's no competitor_id: one synthesis covers the whole market.

Chains after `s5h9i0j1k2l3` (competitor_candidates), which landed on main
ahead of this branch.
"""
from alembic import op
import sqlalchemy as sa


revision = 't6i0j1k2l3m4'
down_revision = 's5h9i0j1k2l3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_synthesis_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id"),
            nullable=True,
        ),
        sa.Column("interaction_id", sa.String(length=128), nullable=True),
        sa.Column("agent", sa.String(length=32), nullable=False, server_default="preview"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("triggered_by", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("window_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("brief", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("sources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("inputs_meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_market_synthesis_reports_run_id",
        "market_synthesis_reports",
        ["run_id"],
    )
    op.create_index(
        "ix_market_synthesis_reports_interaction_id",
        "market_synthesis_reports",
        ["interaction_id"],
    )
    op.create_index(
        "ix_market_synthesis_reports_status",
        "market_synthesis_reports",
        ["status"],
    )
    op.create_index(
        "ix_market_synthesis_reports_started_at",
        "market_synthesis_reports",
        ["started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_synthesis_reports_started_at",
        table_name="market_synthesis_reports",
    )
    op.drop_index(
        "ix_market_synthesis_reports_status",
        table_name="market_synthesis_reports",
    )
    op.drop_index(
        "ix_market_synthesis_reports_interaction_id",
        table_name="market_synthesis_reports",
    )
    op.drop_index(
        "ix_market_synthesis_reports_run_id",
        table_name="market_synthesis_reports",
    )
    op.drop_table("market_synthesis_reports")
