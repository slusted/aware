"""Add findings.digest_threat_level — quality signal from the market-digest pass.

Revision ID: k7e1f2a3b4c5
Revises: j6d0e1f2a3b4
Create Date: 2026-04-20 00:00:00.000000

The market-digest analyzer labels each finding it references as
HIGH / MEDIUM / LOW / NOISE in its prose. We capture that label per
finding as a far stronger "was this useful intel" proxy than raw
materiality (which only grades signal-type potential). Feeds the
history-aware Optimise button — a keyword that consistently produces
HIGH-threat findings is doing its job; one that only ever produces
NOISE or isn't referenced at all is noise and should be replaced.
"""
from alembic import op
import sqlalchemy as sa


revision = 'k7e1f2a3b4c5'
down_revision = 'j6d0e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("digest_threat_level", sa.String(length=16), nullable=True))
        batch.create_index("ix_findings_digest_threat_level", ["digest_threat_level"])


def downgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.drop_index("ix_findings_digest_threat_level")
        batch.drop_column("digest_threat_level")
