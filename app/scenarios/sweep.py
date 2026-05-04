"""LLM classifier sweep.

Picks every Finding with `scenarios_classified_at IS NULL`, calls the
Haiku classifier, writes the proposed evidence rows (unconfirmed), and
stamps the finding so the next sweep skips it.

Hard-stops at the daily $ budget cap so a misconfigured loop or
runaway cost can't bleed money. The cap reads from
`SCENARIOS_CLASSIFIER_DAILY_BUDGET_USD` env var (default $1.00/day).

See docs/scenarios/02-card-tagging.md §"Sweep".
"""
from __future__ import annotations

import os
import traceback
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    Finding,
    Predicate,
    PredicateState,
    PredicateEvidence,
    SourceCredibilityDefault,
    UsageEvent,
)
from .classifier import (
    PredicateRoster,
    ProposedEvidence,
    build_roster,
    build_system_prompt,
    classify_finding,
    get_model,
)


DEFAULT_DAILY_BUDGET_USD = 1.00
DEFAULT_BATCH_LIMIT = 200


class SweepResult(NamedTuple):
    findings_processed: int        # findings the classifier was actually called on
    evidence_proposed: int          # rows inserted into predicate_evidence
    skipped_no_signal: int          # classifier returned [] (or failed)
    skipped_budget: int             # findings the budget cap blocked


# ── Config readers ──────────────────────────────────────────────────────

def daily_budget_usd() -> float:
    raw = os.environ.get("SCENARIOS_CLASSIFIER_DAILY_BUDGET_USD")
    if not raw:
        return DEFAULT_DAILY_BUDGET_USD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_DAILY_BUDGET_USD


def _today_utc_midnight() -> datetime:
    """Return the start of today in UTC, naive (matches the rest of the
    schema which stores naive UTC datetimes via datetime.utcnow)."""
    now = datetime.utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _spent_today_usd(db: Session) -> float:
    """Sum of UsageEvent.cost_usd for rows tagged as scenarios_classifier
    spend, since UTC midnight. SQLite-flavoured json_extract; if the
    extra field is missing we won't match, which is the correct behavior."""
    midnight = _today_utc_midnight()
    spend = (
        db.query(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0))
        .filter(
            UsageEvent.provider == "claude",
            UsageEvent.ts >= midnight,
            func.json_extract(UsageEvent.extra, "$.caller") == "scenarios_classifier",
        )
        .scalar()
    )
    return float(spend or 0.0)


# ── Roster construction (DB-aware wrapper around classifier.build_roster) ──

def load_roster(db: Session) -> PredicateRoster:
    """Pull active predicates + their states out of the DB into the
    roster shape the classifier expects."""
    pred_rows = (
        db.query(Predicate)
        .filter(Predicate.active.is_(True))
        .order_by(Predicate.id)
        .all()
    )
    state_rows_by_pred: dict[int, list[PredicateState]] = {}
    for s in db.query(PredicateState).all():
        state_rows_by_pred.setdefault(s.predicate_id, []).append(s)

    out = []
    for p in pred_rows:
        states = sorted(
            state_rows_by_pred.get(p.id, []),
            key=lambda s: s.ordinal_position,
        )
        out.append({
            "key": p.key,
            "name": p.name,
            "statement": p.statement,
            "category": p.category,
            "states": [
                {"state_key": s.state_key, "label": s.label}
                for s in states
            ],
        })
    return build_roster(out)


# ── Per-finding helpers ─────────────────────────────────────────────────

def _finding_to_dict(f: Finding) -> dict:
    """Reduce the SQLAlchemy row to the dict the classifier expects.
    Pulled out so tests can construct synthetic findings without the
    ORM."""
    return {
        "id": f.id,
        "competitor": f.competitor,
        "source": f.source,
        "signal_type": f.signal_type,
        "title": f.title,
        "summary": f.summary,
        "content": f.content,
        "published_at": f.published_at,
        "created_at": f.created_at,
    }


def _resolve_credibility(
    finding_source: str | None,
    credibility_defaults: dict[str, float],
) -> float:
    """Per-source default. 1.0 fallback for sources we haven't tabled yet
    (CLI-entered evidence behaves the same way)."""
    if not finding_source:
        return 1.0
    return credibility_defaults.get(finding_source, 1.0)


def _proposed_to_evidence_row(
    finding: Finding,
    proposal: ProposedEvidence,
    predicate_id_by_key: dict[str, int],
    credibility_defaults: dict[str, float],
    *,
    now: datetime,
) -> PredicateEvidence | None:
    """Build the unconfirmed PredicateEvidence row. Returns None if the
    predicate_key isn't in the active predicate map (defensive — the
    classifier already validated, but the predicate set could change
    mid-sweep)."""
    predicate_id = predicate_id_by_key.get(proposal.predicate_key)
    if predicate_id is None:
        return None
    observed_at = finding.published_at or finding.created_at or now
    return PredicateEvidence(
        finding_id=finding.id,
        predicate_id=predicate_id,
        target_state_key=proposal.target_state_key,
        direction=proposal.direction,
        strength_bucket=proposal.strength_bucket,
        credibility=_resolve_credibility(finding.source, credibility_defaults),
        classified_by="llm",
        observed_at=observed_at,
        confirmed_at=None,
        notes=proposal.reasoning or None,
    )


# ── Main sweep ──────────────────────────────────────────────────────────

def classify_unclassified(
    db: Session,
    *,
    limit: int | None = None,
    since: datetime | None = None,
    classifier_fn=classify_finding,
    client=None,
) -> SweepResult:
    """Walk findings with `scenarios_classified_at IS NULL`, classify
    each, write evidence rows, stamp the finding.

    `classifier_fn` is injectable so tests can substitute a stub that
    doesn't call out to Anthropic. `client` likewise threads through to
    classifier.classify_finding.

    Caller commits per-finding (see loop body). A partial failure leaves
    already-classified findings stamped — re-running is safe.
    """
    if limit is None:
        limit = DEFAULT_BATCH_LIMIT

    roster = load_roster(db)
    if not roster.predicates:
        # No predicates seeded → nothing to classify against. Bail
        # without stamping (a future seed run will let us classify
        # against the right roster).
        return SweepResult(0, 0, 0, 0)

    system_prompt = build_system_prompt(roster)
    model = get_model()
    budget = daily_budget_usd()

    predicate_id_by_key = {
        p.id: p.key for p in db.query(Predicate).filter(Predicate.active.is_(True)).all()
    }
    # Flip to the shape _proposed_to_evidence_row needs.
    predicate_id_by_key = {v: k for k, v in predicate_id_by_key.items()}

    credibility_defaults = {
        row.source_type: row.credibility
        for row in db.query(SourceCredibilityDefault).all()
    }

    q = db.query(Finding).filter(Finding.scenarios_classified_at.is_(None))
    if since is not None:
        q = q.filter(Finding.created_at >= since)
    findings = q.order_by(Finding.created_at.desc()).limit(limit).all()

    findings_processed = 0
    evidence_proposed = 0
    skipped_no_signal = 0
    skipped_budget = 0

    for f in findings:
        # Pre-call budget check. Each call costs ~$0.001 with cache
        # hits; we check before each call so a one-call overshoot is
        # the worst case.
        if _spent_today_usd(db) >= budget:
            skipped_budget = len(findings) - findings_processed - skipped_no_signal
            break

        try:
            proposals = classifier_fn(
                _finding_to_dict(f),
                roster,
                system_prompt=system_prompt,
                client=client,
                model=model,
            )
        except Exception:
            # Classifier should be failure-soft itself, but defensive
            # try/except so one bad finding doesn't kill the sweep.
            traceback.print_exc()
            proposals = []

        now = datetime.utcnow()
        f.scenarios_classified_at = now

        if not proposals:
            skipped_no_signal += 1
        else:
            for proposal in proposals:
                row = _proposed_to_evidence_row(
                    f, proposal, predicate_id_by_key, credibility_defaults, now=now,
                )
                if row is not None:
                    db.add(row)
                    evidence_proposed += 1
        findings_processed += 1

        # Commit per finding — partial sweep failures leave a clean
        # stamp + matching evidence rows. Mid-sweep crash on finding 50
        # of 200 means findings 1–49 are fully classified and stamped;
        # the next sweep picks up at finding 50.
        db.commit()

    return SweepResult(
        findings_processed=findings_processed,
        evidence_proposed=evidence_proposed,
        skipped_no_signal=skipped_no_signal,
        skipped_budget=skipped_budget,
    )
