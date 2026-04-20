"""Signal stream: typed findings + per-user views + saved filters.

Revision ID: f2c8d4e59a13
Revises: e1f2a3b4c5d6
Create Date: 2026-04-19 00:00:00.000000

Turns the findings table into a signal-stream record by adding signal_type,
payload, and materiality. Adds signal_views (per-user read state) and
saved_filters (named stream views). All additions are nullable or new
tables, so existing rows continue to load and can be backfilled lazily.
"""
from alembic import op
import sqlalchemy as sa


revision = 'f2c8d4e59a13'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("signal_type", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("materiality", sa.Float(), nullable=True))

    op.create_index("ix_findings_signal_type", "findings", ["signal_type"])
    op.create_index("ix_findings_materiality", "findings", ["materiality"])
    # Compound index for the default stream query:
    #   WHERE materiality >= X ORDER BY created_at DESC
    op.create_index(
        "ix_findings_created_materiality",
        "findings",
        ["created_at", "materiality"],
    )

    op.create_table(
        "signal_views",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("finding_id", sa.Integer(), sa.ForeignKey("findings.id"), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("snoozed_until", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "finding_id", name="uq_signal_views_user_finding"),
    )
    op.create_index("ix_signal_views_user_id", "signal_views", ["user_id"])
    op.create_index("ix_signal_views_finding_id", "signal_views", ["finding_id"])

    op.create_table(
        "saved_filters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="private"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_saved_filters_owner_id", "saved_filters", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_saved_filters_owner_id", table_name="saved_filters")
    op.drop_table("saved_filters")

    op.drop_index("ix_signal_views_finding_id", table_name="signal_views")
    op.drop_index("ix_signal_views_user_id", table_name="signal_views")
    op.drop_table("signal_views")

    op.drop_index("ix_findings_created_materiality", table_name="findings")
    op.drop_index("ix_findings_materiality", table_name="findings")
    op.drop_index("ix_findings_signal_type", table_name="findings")
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("materiality")
        batch.drop_column("payload")
        batch.drop_column("signal_type")
