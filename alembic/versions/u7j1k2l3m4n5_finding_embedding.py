"""finding_embedding + taste_embedding

Revision ID: u7j1k2l3m4n5
Revises: t6i0j1k2l3m4
Create Date: 2026-04-25 00:00:00.000000

Adds embedding storage for semantic ranking (docs/ranker/08-semantic-ranking.md):

- findings.embedding (BLOB) + findings.embedding_model — per-finding 512-dim
  float32 vector packed via numpy.tobytes(). NULL means "not embedded yet"
  or "API call failed" — scoring degrades silently in either case.
- user_preference_profile.taste_embedding + three book-keeping columns —
  per-user signed-weighted L2-normalized centroid built by the rollup.
"""
from alembic import op
import sqlalchemy as sa


revision = 'u7j1k2l3m4n5'
down_revision = 't6i0j1k2l3m4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "findings",
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "findings",
        sa.Column("embedding_model", sa.String(length=64), nullable=True),
    )

    op.add_column(
        "user_preference_profile",
        sa.Column("taste_embedding", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "user_preference_profile",
        sa.Column(
            "taste_embedding_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "user_preference_profile",
        sa.Column("taste_embedding_model", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "user_preference_profile",
        sa.Column("taste_embedding_updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_preference_profile", "taste_embedding_updated_at")
    op.drop_column("user_preference_profile", "taste_embedding_model")
    op.drop_column("user_preference_profile", "taste_embedding_count")
    op.drop_column("user_preference_profile", "taste_embedding")
    op.drop_column("findings", "embedding_model")
    op.drop_column("findings", "embedding")
