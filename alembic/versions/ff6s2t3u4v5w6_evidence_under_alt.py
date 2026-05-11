"""predicate_evidence: evidence_under_alt_bucket (Bayesian P(E|¬H) discriminativeness)

Revision ID: ff6s2t3u4v5w6
Revises: cc4mktrels001
Create Date: 2026-05-11 12:00:00.000000

Adds one nullable column to predicate_evidence so the scorer can stamp
the answer to the actual Bayesian question: "if the predicate were in a
different state, how often would I still see a finding like this?"

Buckets:
  rare        — strongly discriminating; this evidence really shows up
                only when the predicate is in this state.
  occasional  — somewhat discriminating; would also appear in other
                states but less often.
  common      — non-discriminating; about as likely under any state of
                the predicate, so the likelihood ratio is ~1.

The math layer multiplier table (added in the follow-up PR) reads this
column. NULL → neutral (1.0), preserving legacy/unscored row behaviour.

Nullable, no backfill — safe to deploy independently of the code that
uses it. Stage-7-style.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'ff6s2t3u4v5w6'
down_revision = 'cc4mktrels001'
branch_labels = None
depends_on = None


_COLUMN = ("evidence_under_alt_bucket", sa.String(length=16))


def _existing_columns(table: str) -> set[str]:
    insp = inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    existing = _existing_columns("predicate_evidence")
    name, type_ = _COLUMN
    if name in existing:
        return
    with op.batch_alter_table("predicate_evidence") as batch:
        batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("predicate_evidence") as batch:
        batch.drop_column(_COLUMN[0])
