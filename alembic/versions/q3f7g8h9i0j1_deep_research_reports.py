"""deep_research_reports

Revision ID: q3f7g8h9i0j1
Revises: p2e6f7a8b9c0
Create Date: 2026-04-23 00:00:00.000000

Adds the `deep_research_reports` table — one row per Gemini Deep Research
run per competitor, append-only. Latest row per competitor_id is the
'current' dossier on the Research tab; older rows are history.
"""
from alembic import op
import sqlalchemy as sa


revision = 'q3f7g8h9i0j1'
down_revision = 'p2e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deep_research_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "competitor_id",
            sa.Integer(),
            sa.ForeignKey("competitors.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id"),
            nullable=True,
        ),
        sa.Column("interaction_id", sa.String(length=128), nullable=True),
        sa.Column("agent", sa.String(length=32), nullable=False, server_default="preview"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("brief", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("sources", sa.JSON(), nullable=False, server_default="[]"),
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
        "ix_deep_research_reports_competitor_id",
        "deep_research_reports",
        ["competitor_id"],
    )
    op.create_index(
        "ix_deep_research_reports_run_id",
        "deep_research_reports",
        ["run_id"],
    )
    op.create_index(
        "ix_deep_research_reports_interaction_id",
        "deep_research_reports",
        ["interaction_id"],
    )
    op.create_index(
        "ix_deep_research_reports_status",
        "deep_research_reports",
        ["status"],
    )
    op.create_index(
        "ix_deep_research_reports_started_at",
        "deep_research_reports",
        ["started_at"],
    )
    op.create_index(
        "ix_deep_research_competitor_started",
        "deep_research_reports",
        ["competitor_id", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deep_research_competitor_started",
        table_name="deep_research_reports",
    )
    op.drop_index(
        "ix_deep_research_reports_started_at",
        table_name="deep_research_reports",
    )
    op.drop_index(
        "ix_deep_research_reports_status",
        table_name="deep_research_reports",
    )
    op.drop_index(
        "ix_deep_research_reports_interaction_id",
        table_name="deep_research_reports",
    )
    op.drop_index(
        "ix_deep_research_reports_run_id",
        table_name="deep_research_reports",
    )
    op.drop_index(
        "ix_deep_research_reports_competitor_id",
        table_name="deep_research_reports",
    )
    op.drop_table("deep_research_reports")
