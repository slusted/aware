"""Manual-evidence CLI for the Scenarios belief engine (Stage 1).

Inserts one PredicateEvidence row, marks it confirmed, and triggers a
single-predicate recompute. Prints the predicate's posterior before and
after so you can sanity-check movement against the math in your head.

Stage 2 replaces this with an LLM-proposed / human-confirms queue UI.
Until then, this is the only way evidence enters the system.

Usage:
    python -m scripts.add_evidence \\
        --predicate p8 \\
        --target-state workflow \\
        --direction support \\
        --strength strong \\
        --finding-id 12345 \\
        --notes "Workday earnings call: ..."

    # Backdated, no source finding:
    python -m scripts.add_evidence \\
        --predicate p1 --target-state agent \\
        --direction support --strength moderate \\
        --observed-at 2026-03-12 \\
        --credibility 0.7 \\
        --notes "OpenAI agent SDK launch"

Direction: support | contradict | neutral
Strength:  weak | moderate | strong
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Predicate,
    PredicateState,
    PredicateEvidence,
    Finding,
    SourceCredibilityDefault,
)
from app.scenarios.service import recompute_predicate  # noqa: E402


VALID_DIRECTIONS = ("support", "contradict", "neutral")
VALID_STRENGTHS = ("weak", "moderate", "strong")


def _parse_observed_at(s: str) -> datetime:
    """Accept YYYY-MM-DD or full ISO. Local-naive datetime to match the
    rest of the schema (datetime.utcnow defaults)."""
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%d")


def _resolve_credibility(
    db, finding: Finding | None, override: float | None
) -> float:
    """Per-row override wins. Otherwise look up the source-type default.
    Otherwise 1.0 (manual)."""
    if override is not None:
        return override
    if finding is not None and finding.source:
        row = (
            db.query(SourceCredibilityDefault)
            .filter(SourceCredibilityDefault.source_type == finding.source)
            .first()
        )
        if row is not None:
            return row.credibility
    return 1.0


def _resolve_observed_at(
    finding: Finding | None, override: datetime | None
) -> datetime:
    """Per-row override wins. Otherwise prefer finding.published_at, else
    finding.created_at, else now."""
    if override is not None:
        return override
    if finding is not None:
        return finding.published_at or finding.created_at or datetime.utcnow()
    return datetime.utcnow()


def _format_posterior(prefix: str, posterior: dict[str, float]) -> str:
    parts = [f"{k}={v:.3f}" for k, v in sorted(posterior.items())]
    return f"{prefix}: " + ", ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Add one piece of evidence and recompute the predicate's posterior.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--predicate", required=True,
                    help="Predicate key, e.g. p8")
    ap.add_argument("--target-state", required=True,
                    help="State key the evidence bears on, e.g. workflow")
    ap.add_argument("--direction", required=True, choices=VALID_DIRECTIONS)
    ap.add_argument("--strength", required=True, choices=VALID_STRENGTHS)
    ap.add_argument("--finding-id", type=int, default=None,
                    help="Source finding id (optional). Defaults credibility "
                         "and observed-at when set.")
    ap.add_argument("--credibility", type=float, default=None,
                    help="Override credibility (0–1). Otherwise: source default, "
                         "else 1.0.")
    ap.add_argument("--observed-at", default=None,
                    help="ISO date or YYYY-MM-DD. Otherwise: finding.published_at "
                         "/ created_at, else now.")
    ap.add_argument("--notes", default=None)
    ap.add_argument("--classified-by", default="manual",
                    choices=("manual", "llm", "user_override"))
    ap.add_argument("--no-recompute", action="store_true",
                    help="Skip the per-predicate recompute. Useful when bulk-"
                         "inserting; run scripts/seed_scenarios + the recompute "
                         "job afterwards.")
    args = ap.parse_args()

    if not (0.0 <= (args.credibility if args.credibility is not None else 0.5) <= 1.0):
        print("ERROR: --credibility must be in [0, 1]", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        pred = (
            db.query(Predicate)
            .filter(Predicate.key == args.predicate)
            .one_or_none()
        )
        if pred is None:
            print(f"ERROR: predicate {args.predicate!r} not found", file=sys.stderr)
            return 2
        valid_states = {
            s.state_key for s in
            db.query(PredicateState).filter(PredicateState.predicate_id == pred.id).all()
        }
        if args.target_state not in valid_states:
            print(
                f"ERROR: target state {args.target_state!r} not in predicate "
                f"{pred.key!r}; valid: {sorted(valid_states)}",
                file=sys.stderr,
            )
            return 2

        finding = None
        if args.finding_id is not None:
            finding = db.get(Finding, args.finding_id)
            if finding is None:
                print(
                    f"ERROR: finding id {args.finding_id} not found",
                    file=sys.stderr,
                )
                return 2

        observed_at = _resolve_observed_at(
            finding,
            _parse_observed_at(args.observed_at) if args.observed_at else None,
        )
        credibility = _resolve_credibility(db, finding, args.credibility)
        now = datetime.utcnow()

        # Snapshot the predicate's current posterior so we can show
        # before/after. Cheap dict copy off the cached column.
        before = {
            s.state_key: s.current_probability
            for s in db.query(PredicateState).filter(PredicateState.predicate_id == pred.id).all()
        }

        ev = PredicateEvidence(
            finding_id=args.finding_id,
            predicate_id=pred.id,
            target_state_key=args.target_state,
            direction=args.direction,
            strength_bucket=args.strength,
            credibility=credibility,
            classified_by=args.classified_by,
            observed_at=observed_at,
            confirmed_at=now,  # CLI-entered = pre-confirmed
            notes=args.notes,
        )
        db.add(ev)
        db.flush()

        print(f"Added evidence id={ev.id} to predicate {pred.key} "
              f"(target={args.target_state}, "
              f"{args.direction}/{args.strength}, "
              f"credibility={credibility:.2f}, "
              f"observed_at={observed_at.isoformat(timespec='seconds')})")

        if args.no_recompute:
            db.commit()
            print("Skipped recompute (--no-recompute set).")
            return 0

        after = recompute_predicate(db, pred.id, now=now)
        db.commit()

        print(_format_posterior("before", before))
        print(_format_posterior("after ", after))
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
