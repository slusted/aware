"""Add users.default_filter_id (per-user default saved stream filter).

Revision ID: h4b8c9d0e1f2
Revises: g3a7b8c9d0e1
Create Date: 2026-04-19 13:00:00.000000

When set, /stream auto-applies the referenced SavedFilter's spec on load
if the request has no filter query params. ON DELETE SET NULL so deleting
a saved filter clears any user defaults pointing at it.
"""
from alembic import op
import sqlalchemy as sa


revision = 'h4b8c9d0e1f2'
down_revision = 'g3a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("default_filter_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_users_default_filter_id",
            "saved_filters",
            ["default_filter_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_default_filter_id", type_="foreignkey")
        batch.drop_column("default_filter_id")
