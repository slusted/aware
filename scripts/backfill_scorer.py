"""One-shot backfill: run the Stage-7 multi-pass scorer over every
existing predicate_evidence row that hasn't been scored yet.

For each row where `scored_at IS NULL` and a `finding_id` is set:
  - Pull the finding + the predicate statement + the target state label.
  - Call app.scenarios.scorer.score_proposal (Sonnet pass; mechanism /
    base rate / counter-evidence / incentive bias).
  - Call app.scenarios.redundancy.redundancy_score (deterministic cosine
    sim; no API).
  - Stamp the eight scorer columns + scored_at on the row.
  - Per-row commit so a partial failure leaves prior progress saved.

After all targeted rows are scored, recompute_predicate is called for
every predicate that received at least one new score so cached
current_probability values reflect the new multipliers.

Usage examples:
    python scripts/backfill_scorer.py --dry-run                  # show what would be done, no API calls
    python scripts/backfill_scorer.py --limit 50                 # score at most 50 rows
    python scripts/backfill_scorer.py --predicate p8             # only one predicate
    python scripts/backfill_scorer.py --since 2026-01-01         # only rows observed_at >= date
    python scripts/backfill_scorer.py                            # everything

Budget guard: respects SCENARIOS_SCORER_DAILY_BUDGET_USD via the same
sweep helper (`_spent_today_usd`). Stops cleanly when over cap.

Idempotent: re-running picks up where it left off (rows with scored_at
set are skipped). Safe to interrupt with Ctrl-C.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Finding,
    Predicate,
    PredicateEvidence,
    PredicateState,
)
from app.scenarios import scorer as _scorer  # noqa: E402
from app.scenarios import redundancy as _redundancy  # noqa: E402
from app.scenarios.service import recompute_predicate  # noqa: E402
from app.scenarios.sweep import (  # noqa: E402
    _spent_today_usd,
    scorer_daily_budget_usd,
)


# ── CLI ─────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill the Stage-7 scorer fields on existing "
                    "predicate_evidence rows."
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of rows to score (default: unlimited).",
    )
    p.add_argument(
        "--predicate", type=str, default=None,
        metavar="KEY",
        help="Only score evidence on this predicate key (e.g. p8). "
             "Default: all active predicates.",
    )
    p.add_argument(
        "--since", type=str, default=None,
        metavar="YYYY-MM-DD",
        help="Only score evidence with observed_at >= this date.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be scored without making API calls or "
             "writing rows.",
    )
    p.add_argument(
        "--no-recompute", action="store_true",
        help="Skip the per-predicate recompute pass at the end. Useful "
             "if you'll trigger recompute_all separately.",
    )
    return p.parse_args()


def _parse_since(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = date.fromisoformat(s)
    except ValueError:
        sys.exit(f"--since must be YYYY-MM-DD, got {s!r}")
    return datetime(d.year, d.month, d.day)


# ── Backfill ────────────────────────────────────────────────────────────

def _build_predicate_ctx(db) -> dict[int, dict]:
    """{predicate_id: {key, statement, state_labels: {state_key: label}}}.
    One pass per process; the scorer prompt needs the human-readable
    statement + state label so the model knows what it's grading."""
    out: dict[int, dict] = {}
    for p in db.query(Predicate).all():
        out[p.id] = {
            "key": p.key,
            "statement": p.statement,
            "state_labels": {},
        }
    for s in db.query(PredicateState).all():
        if s.predicate_id in out:
            out[s.predicate_id]["state_labels"][s.state_key] = s.label
    return out


def _evidence_to_finding_dict(ev: PredicateEvidence, finding: Finding) -> dict:
    """Same shape app.scenarios.classifier produces for the sweep."""
    return {
        "id": finding.id,
        "competitor": finding.competitor,
        "source": finding.source,
        "signal_type": finding.signal_type,
        "title": finding.title,
        "summary": finding.summary,
        "content": finding.content,
        "published_at": finding.published_at,
        "created_at": finding.created_at,
    }


def main() -> int:
    args = _parse_args()
    since = _parse_since(args.since)

    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not has_api_key and not args.dry_run:
        print(
            "ERROR: ANTHROPIC_API_KEY not set. Set it or use --dry-run.",
            file=sys.stderr,
        )
        return 1

    scorer_system_prompt = _scorer.get_system_prompt()
    scorer_model_name = _scorer.get_model()
    scorer_budget = scorer_daily_budget_usd()

    db = SessionLocal()
    try:
        pred_ctx = _build_predicate_ctx(db)
        # Map predicate.key → predicate.id for the --predicate filter.
        key_to_id = {ctx["key"]: pid for pid, ctx in pred_ctx.items()}

        # Build the row query — only unscored rows, with a real finding,
        # newest first so a one-week prefix backfill grabs recent data.
        q = (
            db.query(PredicateEvidence)
            .filter(PredicateEvidence.scored_at.is_(None))
            .filter(PredicateEvidence.finding_id.isnot(None))
            .order_by(PredicateEvidence.observed_at.desc())
        )
        if args.predicate:
            pred_id = key_to_id.get(args.predicate)
            if pred_id is None:
                print(f"ERROR: predicate {args.predicate!r} not found.", file=sys.stderr)
                return 1
            q = q.filter(PredicateEvidence.predicate_id == pred_id)
        if since is not None:
            q = q.filter(PredicateEvidence.observed_at >= since)
        if args.limit:
            q = q.limit(args.limit)

        rows = q.all()
    finally:
        db.close()

    total = len(rows)
    if total == 0:
        print("Nothing to backfill — every targeted row is already scored.")
        return 0

    print(f"Backfill plan: {total} row(s) to score.")
    print(f"  Model:  {scorer_model_name}")
    print(f"  Budget: ${scorer_budget:.2f}/day "
          f"(SCENARIOS_SCORER_DAILY_BUDGET_USD)")
    if args.dry_run:
        print("  Mode:   DRY RUN — no API calls, no writes.")
    print()

    scored_count = 0
    redundancy_only_count = 0
    failed_count = 0
    skipped_budget = 0
    affected_predicate_ids: set[int] = set()

    for i, ev_id in enumerate([r.id for r in rows], start=1):
        # Re-open the session per-row so a long backfill doesn't hold a
        # long-lived transaction open. Per-row commit follows.
        db = SessionLocal()
        try:
            ev = db.get(PredicateEvidence, ev_id)
            if ev is None or ev.scored_at is not None:
                # Race / re-run: another process scored it. Skip.
                continue

            finding = db.get(Finding, ev.finding_id) if ev.finding_id else None
            if finding is None:
                # Orphaned row (finding pruned). Stamp scored_at so we
                # don't re-attempt forever. NULL scorer columns → math
                # falls back to neutral multipliers.
                if not args.dry_run:
                    ev.scored_at = datetime.utcnow()
                    db.commit()
                continue

            ctx = pred_ctx.get(ev.predicate_id)
            if ctx is None:
                # Predicate was deleted. Same skip-and-stamp.
                if not args.dry_run:
                    ev.scored_at = datetime.utcnow()
                    db.commit()
                continue
            target_label = ctx["state_labels"].get(
                ev.target_state_key, ev.target_state_key,
            )

            if args.dry_run:
                print(
                    f"  [{i}/{total}] would score ev#{ev.id} on "
                    f"{ctx['key']}/{ev.target_state_key} "
                    f"({ev.direction}/{ev.strength_bucket})"
                )
                continue

            # Budget gate before each Sonnet call.
            scored = None
            if _spent_today_usd(db, caller="scenarios_scorer") < scorer_budget:
                try:
                    scored = _scorer.score_proposal(
                        _evidence_to_finding_dict(ev, finding),
                        predicate_statement=ctx["statement"],
                        target_state_label=target_label,
                        direction=ev.direction,
                        strength_bucket=ev.strength_bucket,
                        system_prompt=scorer_system_prompt,
                        model=scorer_model_name,
                    )
                except Exception:
                    traceback.print_exc()
                    scored = None
            else:
                skipped_budget += 1

            # Redundancy is free — always run it.
            try:
                redundancy = _redundancy.redundancy_score(
                    db, finding, ev.predicate_id,
                )
            except Exception:
                traceback.print_exc()
                redundancy = None

            now = datetime.utcnow()
            if scored is not None:
                ev.mechanism_present = scored.mechanism_present
                ev.mechanism_type = scored.mechanism_type
                ev.base_rate_bucket = scored.base_rate_bucket
                ev.counter_evidence_strength = scored.counter_evidence_strength
                ev.counter_evidence_example = scored.counter_evidence_example
                ev.incentive_bias = scored.incentive_bias
                ev.scorer_model = scorer_model_name
                ev.scored_at = now
                scored_count += 1
                affected_predicate_ids.add(ev.predicate_id)
            elif redundancy is not None:
                # Sonnet failed (or over budget) but redundancy is real
                # — still stamp scored_at + the redundancy score so
                # we don't re-attempt the row, and the math benefits
                # from the redundancy penalty.
                ev.scored_at = now
                redundancy_only_count += 1
                affected_predicate_ids.add(ev.predicate_id)
            else:
                # Both passes failed; do not stamp scored_at so a future
                # run can retry.
                failed_count += 1
                db.rollback()
                continue

            if redundancy is not None:
                ev.redundancy_score = redundancy

            db.commit()

            if i % 20 == 0 or i == total:
                print(
                    f"  [{i}/{total}] scored={scored_count} "
                    f"redundancy_only={redundancy_only_count} "
                    f"failed={failed_count} "
                    f"skipped_budget={skipped_budget}"
                )
        finally:
            db.close()

    # Post-pass recompute for every predicate that picked up new scores.
    if not args.dry_run and not args.no_recompute and affected_predicate_ids:
        print()
        print(f"Recomputing {len(affected_predicate_ids)} predicate(s) "
              "to flush new multipliers into current_probability...")
        db = SessionLocal()
        try:
            for pid in affected_predicate_ids:
                try:
                    recompute_predicate(db, pid)
                except Exception:
                    traceback.print_exc()
            db.commit()
        finally:
            db.close()

    print()
    print(
        f"Done. scored={scored_count} "
        f"redundancy_only={redundancy_only_count} "
        f"failed={failed_count} "
        f"skipped_budget={skipped_budget}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
