"""predicate_evidence multi-pass scoring columns

Revision ID: ee5r1s2t3u4v5
Revises: dd4q0r1s2t3u4
Create Date: 2026-05-06 00:00:00.000000

Stage 7 — multi-pass evidence scoring (skill/predicate_scorer.md).

Adds nine nullable columns to predicate_evidence so the Sonnet scorer
can stamp mechanism / base_rate / counter_evidence / incentive_bias /
provenance, and the deterministic redundancy pass can stamp the cosine
similarity it found against recent neighbours on the same predicate.

All nullable. NULL → posterior math falls back to a neutral multiplier
(=1.0), so legacy rows from Stage 1 / Stage 2 keep working unchanged.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'ee5r1s2t3u4v5'
down_revision = 'dd4q0r1s2t3u4'
branch_labels = None
depends_on = None


_NEW_COLUMNS = (
    ("mechanism_present", sa.String(length=8)),
    ("mechanism_type", sa.String(length=32)),
    ("base_rate_bucket", sa.String(length=8)),
    ("counter_evidence_strength", sa.String(length=8)),
    ("counter_evidence_example", sa.Text()),
    ("incentive_bias", sa.String(length=8)),
    ("redundancy_score", sa.Float()),
    ("scorer_model", sa.String(length=64)),
    ("scored_at", sa.DateTime()),
)


def _existing_columns(table: str) -> set[str]:
    insp = inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    existing = _existing_columns("predicate_evidence")
    with op.batch_alter_table("predicate_evidence") as batch:
        for name, type_ in _NEW_COLUMNS:
            if name not in existing:
                batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("predicate_evidence") as batch:
        for name, _ in reversed(_NEW_COLUMNS):
            batch.drop_column(name)
