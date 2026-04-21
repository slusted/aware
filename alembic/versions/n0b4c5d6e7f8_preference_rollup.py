"""Ranker preference rollup tables.

Revision ID: n0b4c5d6e7f8
Revises: m9a3b4c5d6e7
Create Date: 2026-04-21 00:00:00.000000

Creates user_preferences_vector (sparse per-user weights, rebuilt by the
nightly rollup) and user_preference_profile (one row per user, holds the
LLM-editable taste_doc + rollup metadata). See
docs/ranker/02-preference-rollup.md.
"""
from alembic import op
import sqlalchemy as sa


revision = 'n0b4c5d6e7f8'
down_revision = 'm9a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_preferences_vector",
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dimension", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("raw_sum", sa.Float(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("positive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("negative_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_event_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "dimension", "key"),
    )
    op.create_index(
        "ix_user_preferences_vector_user_dim",
        "user_preferences_vector",
        ["user_id", "dimension"],
    )
    op.create_index(
        "ix_user_preferences_vector_user_weight",
        "user_preferences_vector",
        ["user_id", sa.text("weight DESC")],
    )

    op.create_table(
        "user_preference_profile",
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("taste_doc", sa.Text(), nullable=True),
        sa.Column("cold_start", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("event_count_30d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_computed_at", sa.DateTime(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_table("user_preference_profile")
    op.drop_index("ix_user_preferences_vector_user_weight", table_name="user_preferences_vector")
    op.drop_index("ix_user_preferences_vector_user_dim", table_name="user_preferences_vector")
    op.drop_table("user_preferences_vector")
