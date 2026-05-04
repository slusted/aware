"""predicates.source + predicates.proposal_metadata

Revision ID: cc3p9q0r1s2t3
Revises: bb2o8p9q0r1s2
Create Date: 2026-05-04 00:00:00.000000

Phase 3a of the IA reshape — predicates become a first-class concept
with a `source` lifecycle. This migration adds the plumbing without
introducing the LLM-suggestion job (3b). Existing rows backfill to
'user'.

  source             — 'user' | 'llm_proposed' | 'llm_promoted'
                       (free-form String, app-level enum so future
                       sources don't need a migration; default 'user')
  proposal_metadata  — JSON. NULL for user-authored predicates.
                       Shape (when llm_proposed/llm_promoted):
                         {
                           "finding_ids": [int],
                           "reason": str,
                           "model": str,
                           "proposed_at": isoformat,
                         }

Backfill: every existing row gets source='user' via the column's
server_default. proposal_metadata stays NULL.

Single-table column add — safe in batch_alter_table on SQLite.
"""
from alembic import op
import sqlalchemy as sa


revision = 'cc3p9q0r1s2t3'
down_revision = 'bb2o8p9q0r1s2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("predicates") as batch:
        batch.add_column(
            sa.Column(
                "source",
                sa.String(length=32),
                nullable=False,
                server_default="user",
            )
        )
        batch.add_column(
            sa.Column("proposal_metadata", sa.JSON(), nullable=True)
        )
    op.create_index(
        "ix_predicates_source",
        "predicates",
        ["source"],
    )


def downgrade() -> None:
    op.drop_index("ix_predicates_source", table_name="predicates")
    with op.batch_alter_table("predicates") as batch:
        batch.drop_column("proposal_metadata")
        batch.drop_column("source")
