"""app_review_sources + app_reviews + review_themes

Revision ID: y1n5o6p7q8r9
Revises: x0m4n5o6p7q8
Create Date: 2026-04-27 00:00:00.000000

Tables for the app-store reviews VoC pipeline (docs/voc/01-app-reviews.md).
A separate pipeline from the existing scanner / customer_watch flow:
reviews live as a corpus, themes hold rolling state, and findings only
emit on theme emergence or shift.

Order matters in upgrade(): review_themes is created first because
app_reviews.theme_id references it. Idempotent against partial failures
in the same way x0m4n5o6p7q8 is — drop-then-create on each table — so a
crash mid-migration on Railway doesn't trap the container in a hot
crash-loop.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'y1n5o6p7q8r9'
down_revision = 'x0m4n5o6p7q8'
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _existing_tables()
    # Drop in reverse-FK order so a half-applied previous run can be
    # cleared cleanly. None of these tables ever shipped, so there's no
    # data to preserve.
    if "app_reviews" in tables:
        op.drop_table("app_reviews")
    if "review_themes" in tables:
        op.drop_table("review_themes")
    if "app_review_sources" in tables:
        op.drop_table("app_review_sources")

    op.create_table(
        "app_review_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competitor_id", sa.Integer(),
                  sa.ForeignKey("competitors.id"), nullable=False),
        sa.Column("store", sa.String(length=16), nullable=False),
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("country", sa.String(length=8), nullable=False, server_default="us"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_ingested_at", sa.DateTime(), nullable=True),
        sa.Column("last_ingested_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("store", "app_id", "country", name="uq_app_source"),
    )
    op.create_index("ix_app_source_competitor",
                    "app_review_sources", ["competitor_id", "enabled"])

    op.create_table(
        "review_themes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competitor_id", sa.Integer(),
                  sa.ForeignKey("competitors.id"), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("sentiment", sa.String(length=16), nullable=False, server_default="mixed"),
        sa.Column("volume_30d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("volume_prev_30d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_review_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("first_seen", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("last_run_id", sa.Integer(),
                  sa.ForeignKey("runs.id"), nullable=True),
    )
    op.create_index("ix_review_themes_competitor_id",
                    "review_themes", ["competitor_id"])
    op.create_index("ix_theme_competitor_status",
                    "review_themes", ["competitor_id", "status"])

    op.create_table(
        "app_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(),
                  sa.ForeignKey("app_review_sources.id"), nullable=False),
        sa.Column("competitor_id", sa.Integer(),
                  sa.ForeignKey("competitors.id"), nullable=False),
        sa.Column("store", sa.String(length=16), nullable=False),
        sa.Column("store_review_id", sa.String(length=128), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("lang", sa.String(length=8), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("theme_id", sa.Integer(),
                  sa.ForeignKey("review_themes.id"), nullable=True),
        sa.UniqueConstraint("store", "store_review_id", name="uq_review_storeid"),
    )
    op.create_index("ix_app_reviews_source_id", "app_reviews", ["source_id"])
    op.create_index("ix_app_reviews_competitor_id", "app_reviews", ["competitor_id"])
    op.create_index("ix_app_reviews_posted_at", "app_reviews", ["posted_at"])
    op.create_index("ix_app_reviews_ingested_at", "app_reviews", ["ingested_at"])
    op.create_index("ix_app_reviews_theme_id", "app_reviews", ["theme_id"])
    op.create_index("ix_review_competitor_posted",
                    "app_reviews", ["competitor_id", "posted_at"])


def downgrade() -> None:
    op.drop_index("ix_review_competitor_posted", table_name="app_reviews")
    op.drop_index("ix_app_reviews_theme_id", table_name="app_reviews")
    op.drop_index("ix_app_reviews_ingested_at", table_name="app_reviews")
    op.drop_index("ix_app_reviews_posted_at", table_name="app_reviews")
    op.drop_index("ix_app_reviews_competitor_id", table_name="app_reviews")
    op.drop_index("ix_app_reviews_source_id", table_name="app_reviews")
    op.drop_table("app_reviews")

    op.drop_index("ix_theme_competitor_status", table_name="review_themes")
    op.drop_index("ix_review_themes_competitor_id", table_name="review_themes")
    op.drop_table("review_themes")

    op.drop_index("ix_app_source_competitor", table_name="app_review_sources")
    op.drop_table("app_review_sources")
