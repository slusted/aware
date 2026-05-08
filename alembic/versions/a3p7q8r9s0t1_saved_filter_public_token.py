"""saved_filters.public_token + public_token_created_at

Revision ID: a3p7q8r9s0t1
Revises: ee5r1s2t3u4v5
Create Date: 2026-05-08 00:00:00.000000

Two new columns on saved_filters for the public-share-link feature
(docs/stream/01-public-share-link.md):
  - public_token: nullable, unique, ~32-char urlsafe string. Granting
    unauthenticated read access to a rendered view at /p/{token}.
  - public_token_created_at: nullable timestamp set when the token is
    minted. Useful for the share UI ("created N days ago") and any
    future expiry policy.

Both columns default null so existing filters behave exactly as before.
Idempotent: safe to re-run if a previous attempt half-applied (the
unique index won't be re-added if it already exists).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'a3p7q8r9s0t1'
down_revision = 'ee5r1s2t3u4v5'
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _existing_indexes(table: str) -> set[str]:
    return {ix["name"] for ix in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    cols = _existing_columns("saved_filters")
    if "public_token" not in cols:
        op.add_column(
            "saved_filters",
            sa.Column("public_token", sa.String(length=64), nullable=True),
        )
    if "public_token_created_at" not in cols:
        op.add_column(
            "saved_filters",
            sa.Column("public_token_created_at", sa.DateTime(), nullable=True),
        )
    indexes = _existing_indexes("saved_filters")
    if "ix_saved_filters_public_token" not in indexes:
        op.create_index(
            "ix_saved_filters_public_token",
            "saved_filters",
            ["public_token"],
            unique=True,
        )


def downgrade() -> None:
    indexes = _existing_indexes("saved_filters")
    if "ix_saved_filters_public_token" in indexes:
        op.drop_index("ix_saved_filters_public_token", table_name="saved_filters")
    cols = _existing_columns("saved_filters")
    if "public_token_created_at" in cols:
        op.drop_column("saved_filters", "public_token_created_at")
    if "public_token" in cols:
        op.drop_column("saved_filters", "public_token")
