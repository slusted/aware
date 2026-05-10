"""saved_filters.public_qa_enabled + public_share_qa_usage table

Revision ID: b4r9s0t1u2v3
Revises: a3p7q8r9s0t1
Create Date: 2026-05-08 00:00:00.000000

Schema for the public-share Q&A agent (docs/stream/02-public-share-qa.md
will land alongside the route — this migration just lays the rails):

  - saved_filters.public_qa_enabled: boolean owner toggle. Defaults
    false so every existing share keeps rendering exactly as before.
    The /p/{token} route only mounts the Q&A panel when this is true.

  - public_share_qa_usage: per-share, per-day token + request counter.
    The Q&A route bumps this row before answering and rejects the
    request when any cap is exceeded. Keyed on saved_filter_id (not
    the token itself) so rotating the token mid-day does not reset
    the day's spend — the budget is "per share per day", and the
    share is the saved-filter row.

Idempotent: safe to re-run if a previous attempt half-applied.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'b4r9s0t1u2v3'
down_revision = 'a3p7q8r9s0t1'
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _existing_indexes(table: str) -> set[str]:
    return {ix["name"] for ix in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    cols = _existing_columns("saved_filters")
    if "public_qa_enabled" not in cols:
        op.add_column(
            "saved_filters",
            sa.Column(
                "public_qa_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    tables = _existing_tables()
    if "public_share_qa_usage" not in tables:
        op.create_table(
            "public_share_qa_usage",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "saved_filter_id",
                sa.Integer(),
                sa.ForeignKey("saved_filters.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("usage_date", sa.Date(), nullable=False),
            sa.Column(
                "input_tokens",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "output_tokens",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "request_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
        )

    indexes = _existing_indexes("public_share_qa_usage") if "public_share_qa_usage" in _existing_tables() else set()
    if "uq_public_share_qa_usage_filter_date" not in indexes:
        op.create_index(
            "uq_public_share_qa_usage_filter_date",
            "public_share_qa_usage",
            ["saved_filter_id", "usage_date"],
            unique=True,
        )


def downgrade() -> None:
    tables = _existing_tables()
    if "public_share_qa_usage" in tables:
        indexes = _existing_indexes("public_share_qa_usage")
        if "uq_public_share_qa_usage_filter_date" in indexes:
            op.drop_index(
                "uq_public_share_qa_usage_filter_date",
                table_name="public_share_qa_usage",
            )
        op.drop_table("public_share_qa_usage")
    cols = _existing_columns("saved_filters")
    if "public_qa_enabled" in cols:
        op.drop_column("saved_filters", "public_qa_enabled")
