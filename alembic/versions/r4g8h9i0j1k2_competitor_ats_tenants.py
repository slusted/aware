"""Competitor.ats_tenants

Revision ID: r4g8h9i0j1k2
Revises: q3f7g8h9i0j1
Create Date: 2026-04-23 00:00:00.000000

Adds the `ats_tenants` JSON column to competitors. List of canonical ATS
tenant URL prefixes (e.g. "boards.greenhouse.io/adeccogroup") used to scope
the hiring sweep to a competitor's own board instead of the ATS root domain
(which hosts every customer's jobs).

Defaults to [] for every existing row. The discovery pass in
app/adapters/ats/discovery.py will populate it on the next scan for
competitors with a careers page configured.
"""
from alembic import op
import sqlalchemy as sa


revision = 'r4g8h9i0j1k2'
down_revision = 'q3f7g8h9i0j1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "competitors",
        sa.Column(
            "ats_tenants",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("competitors") as batch:
        batch.drop_column("ats_tenants")
