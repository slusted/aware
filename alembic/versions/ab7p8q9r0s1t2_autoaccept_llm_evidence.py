"""auto-accept existing LLM-proposed predicate evidence

Revision ID: ab7p8q9r0s1t2
Revises: b4r9s0t1u2v3
Create Date: 2026-05-10 00:00:00.000000

Backfill for the auto-accept policy change. Previously the LLM
classifier (app/scenarios/sweep.py::_proposed_to_evidence_row) created
predicate_evidence rows with confirmed_at=NULL — pending review. After
this revision the classifier sets confirmed_at=now on creation, so
proposals count toward each prediction's posterior immediately and the
user opts-out via Reject instead of opting-in via Confirm.

This migration confirm-stamps the existing pending rows so the math
catches up with the new policy. Rows already user_rejected stay as-is
(rejection is sticky). confirmed_at is set to observed_at — the same
timestamp the classifier would have stamped if auto-accept had been on
when the row was created — preserving chronology in the snapshot
series.

Posteriors stored on PredicateState.current_probability go stale until
the next sweep→recompute cycle (kicked off by the next scan, or by
hitting Recompute on /scenarios). Live queries (header counts,
prediction cards) reflect the new evidence count immediately.
"""
from alembic import op


revision = 'ab7p8q9r0s1t2'
down_revision = 'b4r9s0t1u2v3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE predicate_evidence
        SET confirmed_at = COALESCE(observed_at, CURRENT_TIMESTAMP)
        WHERE confirmed_at IS NULL
          AND classified_by != 'user_rejected'
    """)


def downgrade() -> None:
    # No-op: we can't tell which rows were originally pending vs. originally
    # confirmed — reverting would lose user-confirmed signal too.
    pass
