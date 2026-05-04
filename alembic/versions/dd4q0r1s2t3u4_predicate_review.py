"""predicate_reviews + predicate_proposals + predicate_evidence.fitness*

Revision ID: dd4q0r1s2t3u4
Revises: aa3agentbrnd1
Create Date: 2026-05-04 00:00:00.000000

Stage 6 — monthly ontology hygiene (docs/scenarios/06-predicate-review.md).

Adds:
  * predicate_reviews         — one row per (predicate, run) review pass.
  * predicate_proposals       — queue of refine/rename/reorder/split/
                                reassign/retire actions awaiting human
                                Accept / Reject. merge_with and
                                new_predicate kinds are reserved by the
                                schema but not surfaced in any UI here.
  * predicate_evidence.fitness*       (3 columns)
  * predicates.next_review_due_at     (cooldown after "Looks good — dismiss")

Drop-then-create on each new table so a half-applied previous run on a
Railway box doesn't trap the container in a hot crash-loop. Same idiom
as z2o6p7q8r9s0 / w9l3m4n5o6p7.

Originally landed (#107) with `down_revision = 'cc3p9q0r1s2t3'`, which
collided with the `aa3agentbrnd1` agent-brand migration (#106) that
also chains off `cc3p9q0r1s2t3`. Production crashed at boot with
"multiple heads"; we now chain after `aa3agentbrnd1` so the linear
chain is cc3 → aa3 → dd4.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'dd4q0r1s2t3u4'
down_revision = 'aa3agentbrnd1'
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _existing_columns(table: str) -> set[str]:
    insp = inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    tables = _existing_tables()
    # Drop in reverse-FK order. Neither table has shipped yet, so no
    # data preservation needed.
    if "predicate_proposals" in tables:
        op.drop_table("predicate_proposals")
    if "predicate_reviews" in tables:
        op.drop_table("predicate_reviews")

    op.create_table(
        "predicate_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "predicate_id", sa.Integer(),
            sa.ForeignKey("predicates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id", sa.Integer(),
            sa.ForeignKey("runs.id"), nullable=True,
        ),
        sa.Column(
            "reviewed_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "findings_seen_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "decided_no_change", sa.Boolean(),
            nullable=False, server_default=sa.true(),
        ),
        sa.Column("summary_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "suggested_actions_json", sa.Text(),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "proposal_ids_json", sa.Text(),
            nullable=False, server_default="[]",
        ),
    )
    op.create_index(
        "ix_predicate_reviews_pred_reviewed",
        "predicate_reviews", ["predicate_id", "reviewed_at"],
    )
    op.create_index(
        "ix_predicate_reviews_run", "predicate_reviews", ["run_id"],
    )

    op.create_table(
        "predicate_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "source_predicate_key", sa.String(length=32), nullable=True,
        ),
        sa.Column(
            "target_payload_json", sa.Text(),
            nullable=False, server_default="{}",
        ),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "supporting_finding_ids_json", sa.Text(),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "status", sa.String(length=16),
            nullable=False, server_default="pending",
        ),
        sa.Column(
            "created_at", sa.DateTime(),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column(
            "decided_by", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=True,
        ),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "source_review_id", sa.Integer(),
            sa.ForeignKey("predicate_reviews.id"), nullable=True,
        ),
    )
    op.create_index(
        "ix_predicate_proposals_pred_status",
        "predicate_proposals", ["source_predicate_key", "status"],
    )
    op.create_index(
        "ix_predicate_proposals_status_created",
        "predicate_proposals", ["status", "created_at"],
    )

    # Three columns on predicate_evidence + a cooldown column on predicates.
    # batch_alter_table so SQLite is happy with the ALTER.
    ev_cols = _existing_columns("predicate_evidence")
    with op.batch_alter_table("predicate_evidence") as batch:
        if "fitness" not in ev_cols:
            batch.add_column(sa.Column("fitness", sa.String(length=16), nullable=True))
        if "fitness_read_as" not in ev_cols:
            batch.add_column(sa.Column("fitness_read_as", sa.Text(), nullable=True))
        if "fitness_reviewed_at" not in ev_cols:
            batch.add_column(sa.Column("fitness_reviewed_at", sa.DateTime(), nullable=True))

    pred_cols = _existing_columns("predicates")
    with op.batch_alter_table("predicates") as batch:
        if "next_review_due_at" not in pred_cols:
            batch.add_column(sa.Column("next_review_due_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("predicates") as batch:
        batch.drop_column("next_review_due_at")
    with op.batch_alter_table("predicate_evidence") as batch:
        batch.drop_column("fitness_reviewed_at")
        batch.drop_column("fitness_read_as")
        batch.drop_column("fitness")

    op.drop_index(
        "ix_predicate_proposals_status_created",
        table_name="predicate_proposals",
    )
    op.drop_index(
        "ix_predicate_proposals_pred_status",
        table_name="predicate_proposals",
    )
    op.drop_table("predicate_proposals")

    op.drop_index("ix_predicate_reviews_run", table_name="predicate_reviews")
    op.drop_index(
        "ix_predicate_reviews_pred_reviewed", table_name="predicate_reviews",
    )
    op.drop_table("predicate_reviews")
