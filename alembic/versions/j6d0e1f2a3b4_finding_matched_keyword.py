"""Add findings.matched_keyword — which configured keyword produced each hit.

Revision ID: j6d0e1f2a3b4
Revises: i5c9d0e1f2a3
Create Date: 2026-04-20 00:00:00.000000

Feeds the history-aware Optimise button on the competitor edit page.
By logging the exact keyword that produced each finding at scan time,
the tuning agent can read per-keyword materiality distributions instead
of inferring attribution from title/content substrings. NULL on rows
from non-keyword sources (careers, ATS boards, customer sweep) and on
legacy rows scanned before this column existed.
"""
from alembic import op
import sqlalchemy as sa


revision = 'j6d0e1f2a3b4'
down_revision = 'i5c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("matched_keyword", sa.String(length=255), nullable=True))
        batch.create_index("ix_findings_matched_keyword", ["matched_keyword"])


def downgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.drop_index("ix_findings_matched_keyword")
        batch.drop_column("matched_keyword")
