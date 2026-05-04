"""findings.scenarios_classified_at

Revision ID: bb2o8p9q0r1s2
Revises: aa1n7o8p9q0r1
Create Date: 2026-05-04 00:00:00.000000

Adds a single nullable DateTime column to `findings` so the Stage-2
classifier sweep (docs/scenarios/02-card-tagging.md) can mark which
findings the LLM has already looked at — even when zero evidence
rows resulted. NULL = not yet classified; sweep picks these up.

Single-column add, safe in batch_alter_table mode on SQLite.
"""
from alembic import op
import sqlalchemy as sa


revision = 'bb2o8p9q0r1s2'
down_revision = 'aa1n7o8p9q0r1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(
            sa.Column("scenarios_classified_at", sa.DateTime(), nullable=True),
        )
    op.create_index(
        "ix_findings_scenarios_classified_at",
        "findings",
        ["scenarios_classified_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_findings_scenarios_classified_at",
        table_name="findings",
    )
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("scenarios_classified_at")
