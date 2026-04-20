"""Real authentication: password hashes + server-side sessions.

Revision ID: l8f2a3b4c6d7
Revises: k7e1f2a3b4c5
Create Date: 2026-04-20 00:00:00.000000

Adds password_hash / is_active / last_login_at to users, and creates the
auth_sessions table. The legacy `admin@local` stub row (if present) is
deactivated so nobody can accidentally land on it after the swap — a
real admin must register via /setup.
"""
from alembic import op
import sqlalchemy as sa


revision = 'l8f2a3b4c6d7'
down_revision = 'k7e1f2a3b4c5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("password_hash", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))

    op.create_table(
        "auth_sessions",
        sa.Column("token", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])

    # Quarantine the pre-auth stub user. Can't delete it — existing FKs
    # (documents.uploaded_by, signal_views.user_id, saved_filters.owner_id)
    # may reference it. Flipping is_active keeps referential integrity while
    # ensuring nobody can log in as it.
    op.execute("UPDATE users SET is_active = 0 WHERE email = 'admin@local' AND password_hash IS NULL")


def downgrade() -> None:
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_login_at")
        batch.drop_column("is_active")
        batch.drop_column("password_hash")
