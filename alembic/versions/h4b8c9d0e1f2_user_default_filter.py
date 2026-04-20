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
    # Plain ADD COLUMN without the FK constraint. SQLite can't add a FK to an
    # existing table via ALTER — it would need batch-mode table rewrite, which
    # was crash-looping Railway. The relationship is defined in the ORM model
    # (User.default_filter_id → SavedFilter) and enforced there; DB-level
    # CASCADE isn't load-bearing because foreign_keys pragma isn't on.
    op.execute("ALTER TABLE users ADD COLUMN default_filter_id INTEGER")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_default_filter_id", type_="foreignkey")
        batch.drop_column("default_filter_id")
