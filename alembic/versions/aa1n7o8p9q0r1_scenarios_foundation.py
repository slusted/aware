"""scenarios foundation: predicates, scenarios, evidence, snapshots, config

Revision ID: aa1n7o8p9q0r1
Revises: z2o6p7q8r9s0
Create Date: 2026-05-04 00:00:00.000000

Tables for the Scenarios belief engine (docs/scenarios/01-foundation.md).
Stage 1 is headless — no UI, no LLM mapping, no scanner hook. Manual
evidence only via scripts/add_evidence.py.

Order matters in upgrade() because of FKs:
  predicates → predicate_states
  scenarios + predicates → scenario_predicate_links
  predicates + findings → predicate_evidence
  predicates + runs → predicate_posterior_snapshots

The three config tables (evidence_likelihood_ratios,
source_credibility_defaults, scenario_settings) have no FKs; they're
seeded by scripts/seed_scenarios.py and edited via SQL in Stage 1.

Idempotent against partial failures the same way z2o6p7q8r9s0 is —
drop-then-create on each table, in reverse FK order. None of these
tables ever shipped, so there's no data to preserve.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'aa1n7o8p9q0r1'
down_revision = 'z2o6p7q8r9s0'
branch_labels = None
depends_on = None


SCENARIO_TABLES_REVERSE_FK_ORDER = [
    "predicate_posterior_snapshots",
    "predicate_evidence",
    "scenario_predicate_links",
    "scenario_settings",
    "source_credibility_defaults",
    "evidence_likelihood_ratios",
    "scenarios",
    "predicate_states",
    "predicates",
]


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _existing_tables()
    for name in SCENARIO_TABLES_REVERSE_FK_ORDER:
        if name in tables:
            op.drop_table(name)

    op.create_table(
        "predicates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("decay_half_life_days", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("key", name="uq_predicates_key"),
    )
    op.create_index("ix_predicates_key", "predicates", ["key"])
    op.create_index("ix_predicates_category", "predicates", ["category"])
    op.create_index("ix_predicates_active", "predicates", ["active"])

    op.create_table(
        "predicate_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("predicate_id", sa.Integer(),
                  sa.ForeignKey("predicates.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("state_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("ordinal_position", sa.Integer(), nullable=False),
        sa.Column("prior_probability", sa.Float(), nullable=False),
        sa.Column("current_probability", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("predicate_id", "state_key", name="uq_predicate_states_pred_key"),
    )
    op.create_index("ix_predicate_states_predicate_id",
                    "predicate_states", ["predicate_id"])

    op.create_table(
        "scenarios",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("key", name="uq_scenarios_key"),
    )
    op.create_index("ix_scenarios_key", "scenarios", ["key"])
    op.create_index("ix_scenarios_active", "scenarios", ["active"])

    op.create_table(
        "scenario_predicate_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scenario_id", sa.Integer(),
                  sa.ForeignKey("scenarios.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("predicate_id", sa.Integer(),
                  sa.ForeignKey("predicates.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("required_state_key", sa.String(length=64), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.UniqueConstraint("scenario_id", "predicate_id", name="uq_scenario_predicate"),
    )
    op.create_index("ix_scenario_predicate_links_scenario_id",
                    "scenario_predicate_links", ["scenario_id"])
    op.create_index("ix_scenario_predicate_links_predicate_id",
                    "scenario_predicate_links", ["predicate_id"])

    op.create_table(
        "predicate_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("finding_id", sa.Integer(),
                  sa.ForeignKey("findings.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("predicate_id", sa.Integer(),
                  sa.ForeignKey("predicates.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("target_state_key", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("strength_bucket", sa.String(length=16), nullable=False),
        sa.Column("credibility", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("classified_by", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("observed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_predicate_evidence_finding_id",
                    "predicate_evidence", ["finding_id"])
    op.create_index("ix_predicate_evidence_predicate_id",
                    "predicate_evidence", ["predicate_id"])
    op.create_index("ix_predicate_evidence_classified_by",
                    "predicate_evidence", ["classified_by"])
    op.create_index("ix_predicate_evidence_observed_at",
                    "predicate_evidence", ["observed_at"])
    op.create_index("ix_predicate_evidence_confirmed_at",
                    "predicate_evidence", ["confirmed_at"])
    op.create_index("ix_predicate_evidence_pred_observed",
                    "predicate_evidence", ["predicate_id", "observed_at"])

    op.create_table(
        "predicate_posterior_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("predicate_id", sa.Integer(),
                  sa.ForeignKey("predicates.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("state_key", sa.String(length=64), nullable=False),
        sa.Column("probability", sa.Float(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("run_id", sa.Integer(),
                  sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_posterior_snap_predicate_id",
                    "predicate_posterior_snapshots", ["predicate_id"])
    op.create_index("ix_posterior_snap_run_id",
                    "predicate_posterior_snapshots", ["run_id"])
    op.create_index("ix_posterior_snap_computed_at",
                    "predicate_posterior_snapshots", ["computed_at"])
    op.create_index("ix_posterior_snap_pred_computed",
                    "predicate_posterior_snapshots", ["predicate_id", "computed_at"])

    op.create_table(
        "evidence_likelihood_ratios",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("strength_bucket", sa.String(length=16), nullable=False),
        sa.Column("multiplier", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("direction", "strength_bucket", name="uq_likelihood_dir_strength"),
    )

    op.create_table(
        "source_credibility_defaults",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("credibility", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_type", name="uq_source_credibility_source_type"),
    )
    op.create_index("ix_source_credibility_source_type",
                    "source_credibility_defaults", ["source_type"])

    op.create_table(
        "scenario_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    for name in SCENARIO_TABLES_REVERSE_FK_ORDER:
        op.drop_table(name)
