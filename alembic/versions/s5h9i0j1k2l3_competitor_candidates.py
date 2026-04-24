"""competitor_candidates

Revision ID: s5h9i0j1k2l3
Revises: r4g8h9i0j1k2
Create Date: 2026-04-24 00:00:00.000000

Adds the `competitor_candidates` table — one row per candidate surfaced
by a discovery run. Append-only; status transitions ('suggested' →
'dismissed' | 'adopted') are the only mutation.
"""
from alembic import op
import sqlalchemy as sa


revision = 's5h9i0j1k2l3'
down_revision = 'r4g8h9i0j1k2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "competitor_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("homepage_domain", sa.String(length=255), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("one_line_why", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="suggested",
        ),
        sa.Column("run_hint", sa.Text(), nullable=True),
        sa.Column(
            "adopted_competitor_id",
            sa.Integer(),
            sa.ForeignKey("competitors.id"),
            nullable=True,
        ),
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
        sa.Column("dismissed_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index(
        "ix_competitor_candidates_run_id",
        "competitor_candidates",
        ["run_id"],
    )
    op.create_index(
        "ix_competitor_candidates_homepage_domain",
        "competitor_candidates",
        ["homepage_domain"],
    )
    op.create_index(
        "ix_competitor_candidates_status",
        "competitor_candidates",
        ["status"],
    )
    op.create_index(
        "ix_competitor_candidates_adopted_competitor_id",
        "competitor_candidates",
        ["adopted_competitor_id"],
    )
    op.create_index(
        "ix_competitor_candidates_created_at",
        "competitor_candidates",
        ["created_at"],
    )
    op.create_index(
        "ix_competitor_candidates_status_created",
        "competitor_candidates",
        ["status", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_competitor_candidates_status_created",
        table_name="competitor_candidates",
    )
    op.drop_index(
        "ix_competitor_candidates_created_at",
        table_name="competitor_candidates",
    )
    op.drop_index(
        "ix_competitor_candidates_adopted_competitor_id",
        table_name="competitor_candidates",
    )
    op.drop_index(
        "ix_competitor_candidates_status",
        table_name="competitor_candidates",
    )
    op.drop_index(
        "ix_competitor_candidates_homepage_domain",
        table_name="competitor_candidates",
    )
    op.drop_index(
        "ix_competitor_candidates_run_id",
        table_name="competitor_candidates",
    )
    op.drop_table("competitor_candidates")
