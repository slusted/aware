"""agent_brand single-row table

Revision ID: aa3agentbrnd1
Revises: cc3p9q0r1s2t3
Create Date: 2026-05-04 00:00:00.000000

App-wide configurable agent name + avatar version. Single-row table
(id=1). Avatar bytes live on disk under DATA_DIR/uploads/agent.png;
only the version counter is in the DB so cache-busting the served URL
is a one-line bump.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'aa3agentbrnd1'
down_revision = 'cc3p9q0r1s2t3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "agent_brand" in tables:
        # Drop-then-create so a half-applied previous run on Railway can
        # be cleared without trapping the container in a hot crash-loop.
        # No data to preserve — table only holds defaults until the user
        # customises it via /settings/agent.
        op.drop_table("agent_brand")

    op.create_table(
        "agent_brand",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, server_default="Flo"),
        sa.Column("avatar_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # Seed the single row so reads never need to handle "no row yet".
    op.execute("INSERT INTO agent_brand (id, name, avatar_version) VALUES (1, 'Flo', 0)")


def downgrade() -> None:
    op.drop_table("agent_brand")
