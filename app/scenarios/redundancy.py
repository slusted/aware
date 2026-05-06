"""Deterministic redundancy pass for the Scenarios scorer.

The spec wants a "novelty vs repetition" check on every new evidence row
so that the third blog post about the same launch doesn't move a
predicate as much as the first. The chat philosophy was "an LLM pass" —
but this is exactly what cosine similarity over finding embeddings is
good at, so we skip the LLM entirely. Cheap, deterministic, no API
budget.

Single function — `redundancy_score` — returns a float in [0, 1]:

  0.0  → fully novel (no embedded neighbour over threshold within window)
  1.0  → near-duplicate of an already-classified finding on this predicate

The multiplier in posterior.py reads this and applies a penalty so a
score of 1.0 halves the evidence's weight; 0.0 leaves it unchanged.

Falls back to 0.0 (no penalty) on any failure path — missing embedding,
no neighbours, dimension mismatch — so a model swap or incomplete
backfill can't crash the sweep.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
from sqlalchemy.orm import Session

from ..adapters import voyage as _voyage
from ..models import Finding, PredicateEvidence


# Cosine sim below this contributes 0; at 1.0 contributes 1.0; linear
# scale in between so the penalty grows smoothly with similarity.
_SIM_THRESHOLD = 0.7
_LOOKBACK_DAYS = 30
_MAX_NEIGHBOURS_SCANNED = 50


def _normalize(vec: np.ndarray) -> np.ndarray | None:
    """Unit-norm a vector. Returns None for zero vectors (the dot
    product is meaningless and we'd divide by zero)."""
    norm = float(np.linalg.norm(vec))
    if norm <= 0:
        return None
    return vec / norm


def redundancy_score(
    db: Session,
    finding: Finding,
    predicate_id: int,
    *,
    now: datetime | None = None,
    window_days: int = _LOOKBACK_DAYS,
    sim_threshold: float = _SIM_THRESHOLD,
) -> float:
    """Cosine similarity between this finding's embedding and the
    embeddings of recent findings already classified onto the same
    predicate. Returns the linearly-scaled max similarity, or 0.0 if no
    neighbours / no embedding.

    Scope is intentionally per-predicate — two findings about the same
    launch can be redundant for one predicate and complementary for
    another, so we don't dedup at finding level."""
    if now is None:
        now = datetime.utcnow()
    if finding is None:
        return 0.0

    target = _voyage.unpack(getattr(finding, "embedding", None))
    if target is None:
        return 0.0
    target = _normalize(target)
    if target is None:
        return 0.0

    cutoff = now - timedelta(days=window_days)

    # Recent confirmed-or-pending evidence on this predicate, joined to
    # findings (so we can pull embeddings). Exclude this finding itself
    # so a re-scan / re-classification doesn't compare to its own row.
    rows = (
        db.query(PredicateEvidence, Finding)
        .join(Finding, Finding.id == PredicateEvidence.finding_id)
        .filter(PredicateEvidence.predicate_id == predicate_id)
        .filter(PredicateEvidence.observed_at >= cutoff)
        .filter(PredicateEvidence.classified_by != "user_rejected")
        .filter(Finding.id != finding.id)
        .filter(Finding.embedding.isnot(None))
        .order_by(PredicateEvidence.observed_at.desc())
        .limit(_MAX_NEIGHBOURS_SCANNED)
        .all()
    )
    if not rows:
        return 0.0

    best_sim = 0.0
    for _ev, neighbour in rows:
        nvec = _voyage.unpack(neighbour.embedding)
        if nvec is None:
            continue
        nvec = _normalize(nvec)
        if nvec is None:
            continue
        sim = float(np.dot(target, nvec))
        if sim > best_sim:
            best_sim = sim

    if best_sim <= sim_threshold:
        return 0.0
    # Linear scale: sim_threshold → 0, 1.0 → 1.0
    span = 1.0 - sim_threshold
    if span <= 0:
        return 1.0
    return min(1.0, max(0.0, (best_sim - sim_threshold) / span))
