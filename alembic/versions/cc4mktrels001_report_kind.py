"""reports.kind + merge alembic heads

Revision ID: cc4mktrels001
Revises: ('ab7p8q9r0s1t2', 'b4r9s0t1u2v3')
Create Date: 2026-05-11 00:00:00.000000

Adds a `kind` discriminator to the `reports` table so the market-brief
page can carry more than one flavour of long-form report (digest vs the
new Product Releases brief — docs/market/02-product-releases.md). All
existing rows are digests, so they backfill to "market_digest".

Doubles as a merge revision: prior to this point the alembic graph had
two unmerged heads (ab7p8q9r0s1t2 and b4r9s0t1u2v3). Listing both as
down_revision collapses them into a single head going forward so
`alembic upgrade head` stops being ambiguous.

Idempotent: drops + recreates the partial index helper if needed.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'cc4mktrels001'
down_revision = ('ab7p8q9r0s1t2', 'b4r9s0t1u2v3')
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _existing_indexes(table: str) -> set[str]:
    return {ix["name"] for ix in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    cols = _existing_columns("reports")
    if "kind" not in cols:
        op.add_column(
            "reports",
            sa.Column(
                "kind",
                sa.String(length=32),
                nullable=False,
                server_default="market_digest",
            ),
        )
    # Drop the server_default once the column is in place — model defines
    # the runtime default; the server-side one was only for the backfill.
    with op.batch_alter_table("reports") as batch:
        batch.alter_column("kind", server_default=None)

    indexes = _existing_indexes("reports")
    if "ix_reports_kind" not in indexes:
        op.create_index("ix_reports_kind", "reports", ["kind"])


def downgrade() -> None:
    indexes = _existing_indexes("reports")
    if "ix_reports_kind" in indexes:
        op.drop_index("ix_reports_kind", table_name="reports")
    cols = _existing_columns("reports")
    if "kind" in cols:
        op.drop_column("reports", "kind")
