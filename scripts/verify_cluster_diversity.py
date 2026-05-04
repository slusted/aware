"""End-to-end verification for the cluster + diversity presentation
layer (docs/ranker/06-cluster-diversity.md).

Pure in-memory test over app/ranker/present.py — no DB, no FastAPI. The
layer itself is a pure function so the test harness stays minimal.

Usage:
    python scripts/verify_cluster_diversity.py
Exit code: 0 on all pass, 1 on any fail.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

# Windows default cp1252 console can't render the arrow glyphs used in
# check names. Force utf-8 where the stream supports it.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app import models  # noqa: E402
from app.adapters import voyage as _voyage  # noqa: E402
from app.ranker import config as rcfg  # noqa: E402
from app.ranker.present import (  # noqa: E402
    ClusterCard,
    cluster,
    default_score,
    diversify,
    lead_findings,
    present,
    title_jaccard,
)


# ── Harness ─────────────────────────────────────────────────────────

_passes = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f"  — {detail}"
    print(line)
    if ok:
        _passes += 1
    else:
        _failures.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


NOW = datetime(2026, 4, 22, 12, 0, 0)


def mk(
    *,
    id: int,
    title: str | None,
    competitor: str = "Indeed",
    signal_type: str | None = "new_hire",
    topic: str | None = None,
    source: str = "news",
    materiality: float | None = 0.5,
    age_days: float = 0.0,
    embedding: "np.ndarray | None" = None,
) -> models.Finding:
    """Detached Finding, never hits a DB. Uses bare constructor so
    SQLAlchemy stays out of the way. Pass `embedding` (an L2-normalized
    float32 ndarray of length EMBEDDING_DIM) to exercise the cosine
    branch of the clusterer; leave None to exercise the Jaccard
    fallback."""
    f = models.Finding()
    f.id = id
    f.competitor = competitor
    f.signal_type = signal_type
    f.topic = topic
    f.source = source
    f.title = title
    f.materiality = materiality
    f.created_at = NOW - timedelta(days=age_days)
    f.hash = f"h{id}"
    if embedding is not None:
        f.embedding = _voyage.pack(embedding)
        f.embedding_model = rcfg.EMBEDDING_MODEL
    else:
        f.embedding = None
        f.embedding_model = None
    return f


def vec(*, axis: int, weight: float = 1.0) -> np.ndarray:
    """Build a deterministic L2-normalized vector that lives mostly on
    one axis. Two vectors built with the same `axis` cosine-cluster
    together; different axes stay below the cosine threshold. `weight`
    in [0, 1] tunes how strongly the vector points along `axis` — 1.0
    is pure, 0.5 leans toward axis but mixes the rest in evenly."""
    arr = np.zeros(rcfg.EMBEDDING_DIM, dtype=np.float32)
    arr[axis] = weight
    if weight < 1.0:
        # Spread the remainder across the other components so the
        # vector still has full dimensionality before normalization.
        leftover = (1.0 - weight) / (rcfg.EMBEDDING_DIM - 1)
        for i in range(rcfg.EMBEDDING_DIM):
            if i != axis:
                arr[i] = leftover
    norm = float(np.linalg.norm(arr))
    return (arr / norm).astype(np.float32, copy=False)


def score_now(f: models.Finding) -> float:
    return default_score(f, now=NOW)


# ── §1: Title tokenization + Jaccard ────────────────────────────────
section("Title Jaccard")

check("identical titles → jaccard 1.0",
      title_jaccard("Indeed launches new pricing tier",
                    "Indeed launches new pricing tier") == 1.0)

# Real-world near-duplicate pair.
j = title_jaccard(
    "Indeed launches new pricing tier",
    "Indeed rolls out new pricing",
)
check("near-duplicate headlines cluster (jaccard >= threshold)",
      j >= rcfg.CLUSTER_JACCARD_THRESHOLD,
      detail=f"jaccard={j:.3f}")

# Merely related → below threshold.
j2 = title_jaccard(
    "Indeed launches pricing changes",
    "Indeed launches recruiter platform",
)
check("tangentially related headlines stay separate",
      j2 < rcfg.CLUSTER_JACCARD_THRESHOLD,
      detail=f"jaccard={j2:.3f}")

check("empty title → jaccard 0.0",
      title_jaccard("", "Indeed launches pricing") == 0.0)

check("None title → jaccard 0.0",
      title_jaccard(None, "Indeed launches pricing") == 0.0)

# Stopwords don't artificially inflate overlap.
j3 = title_jaccard("the a an of to", "Indeed launches pricing")
check("stopword-only title → jaccard 0.0", j3 == 0.0)


# ── §2: Clustering rules ────────────────────────────────────────────
section("Clustering")

# Near-duplicates in same bucket → one cluster.
a = mk(id=1, title="Indeed launches new pricing tier", materiality=0.6)
b = mk(id=2, title="Indeed rolls out new pricing", materiality=0.4)
cards = cluster([a, b], score_fn=score_now)
check("same (competitor, signal_type) + near-duplicate title → cluster of 2",
      len(cards) == 1 and cards[0].size == 2)
check("lead is highest-scoring member",
      cards and cards[0].lead.id == 1)

# Different competitors → stay separate even with identical titles.
c = mk(id=3, title="Hiring push announced", competitor="Indeed")
d = mk(id=4, title="Hiring push announced", competitor="LinkedIn")
cards = cluster([c, d], score_fn=score_now)
check("different competitors with identical titles → two clusters",
      len(cards) == 2 and all(c.size == 1 for c in cards))

# Same competitor + duplicate title across signal_types → ONE cluster.
# Bucket key dropped signal_type so that classifier disagreement on the
# same real-world event (e.g. one row tagged `news`, another `funding`)
# can no longer split a cluster.
e = mk(id=5, title="Indeed launches pricing", signal_type="product_launch")
f = mk(id=6, title="Indeed launches pricing", signal_type="new_hire")
cards = cluster([e, f], score_fn=score_now)
check("same competitor, different signal_type, identical title → one cluster",
      len(cards) == 1 and cards[0].size == 2)

# Transitive clustering via union-find: A~B, B~C, but A!~C directly.
t1 = mk(id=7, title="Indeed announces partnership with Acme")
t2 = mk(id=8, title="Indeed partnership with Acme revealed")    # ~ t1
t3 = mk(id=9, title="Partnership Acme revealed officially")     # ~ t2 but not t1
cards = cluster([t1, t2, t3], score_fn=score_now)
# Verify t1~t2 and t2~t3 above threshold, t1~t3 below.
check("setup: t1~t2 above threshold",
      title_jaccard(t1.title, t2.title) >= rcfg.CLUSTER_JACCARD_THRESHOLD)
check("setup: t2~t3 above threshold",
      title_jaccard(t2.title, t3.title) >= rcfg.CLUSTER_JACCARD_THRESHOLD)
check("transitive clustering merges all three",
      len(cards) == 1 and cards[0].size == 3)

# NULL title → singleton.
n = mk(id=10, title=None)
cards = cluster([n, mk(id=11, title="Some story here")], score_fn=score_now)
check("NULL title → own singleton cluster",
      len(cards) == 2 and all(c.size == 1 for c in cards))

# Lead tie-break: same score, newer wins.
old = mk(id=12, title="Indeed raises funding round", materiality=0.5, age_days=10)
new = mk(id=13, title="Indeed raises funding round", materiality=0.5, age_days=0)
# With standin_score, recency matters; the newer card should score higher.
# But force an exact-tie via score_fn to exercise the tiebreak.
def tied_score(_f: models.Finding) -> float: return 0.5  # noqa: E704
cards = cluster([old, new], score_fn=tied_score)
check("tie on score → newer finding is the cluster lead",
      len(cards) == 1 and cards[0].lead.id == 13)

# Empty input.
check("empty input → empty cluster list", cluster([], score_fn=score_now) == [])


# ── §3: MMR diversification ─────────────────────────────────────────
section("MMR diversification")

# Build 4 clusters: three Indeed×new_hire (highly similar to each other),
# one LinkedIn×product_launch (dissimilar to all).
def mk_card(
    *,
    id: int,
    competitor: str,
    signal_type: str,
    score: float,
    topic: str | None = None,
) -> ClusterCard:
    f = mk(id=id, title=f"Story {id}", competitor=competitor,
           signal_type=signal_type, topic=topic)
    return ClusterCard(lead=f, members=(f,), score=score)


c1 = mk_card(id=101, competitor="Indeed", signal_type="new_hire", score=1.0)
c2 = mk_card(id=102, competitor="Indeed", signal_type="new_hire", score=0.95)
c3 = mk_card(id=103, competitor="Indeed", signal_type="new_hire", score=0.90)
c4 = mk_card(id=104, competitor="LinkedIn", signal_type="product_launch", score=0.80)

# Without MMR: order would be c1, c2, c3, c4.
# With MMR (λ=0.7): c1 picked first; second slot should prefer c4 even
# though c2 scores higher, because c2 has 0.8 similarity to c1 while c4
# has 0.0.
out = diversify([c1, c2, c3, c4], window=4, lambda_=0.7)
check("MMR pushes dissimilar cluster up past near-duplicates",
      [c.lead.id for c in out[:2]] == [101, 104],
      detail=f"got order {[c.lead.id for c in out]}")

# Verify the MMR-2 math explicitly.
# For c2: λ*0.95 − (1-λ)*0.8  = 0.665 − 0.24  = 0.425
# For c4: λ*0.80 − (1-λ)*0.0  = 0.560 − 0.0   = 0.560
# So c4 wins slot 2 — matches expectation above.

# MMR only affects top-N; beyond window keeps score order.
tail1 = mk_card(id=201, competitor="Indeed", signal_type="new_hire", score=0.70)
tail2 = mk_card(id=202, competitor="Indeed", signal_type="new_hire", score=0.60)
out = diversify([c1, c2, c3, c4, tail1, tail2], window=2, lambda_=0.7)
check("beyond mmr_window: pure score-desc order is preserved",
      [c.lead.id for c in out[2:]] == [103, 104, 201, 202],
      detail=f"got tail {[c.lead.id for c in out[2:]]}")

# With λ=1.0 MMR collapses to pure relevance → no diversification.
out = diversify([c1, c2, c3, c4], window=4, lambda_=1.0)
check("lambda=1.0 → pure score order, no diversification",
      [c.lead.id for c in out] == [101, 102, 103, 104])

# Empty input.
check("diversify empty list → empty list", diversify([]) == [])

# Window=0 → no-op (preserves input order).
out = diversify([c1, c2, c3, c4], window=0)
check("window=0 → input preserved",
      [c.lead.id for c in out] == [101, 102, 103, 104])


# ── §4: End-to-end present() ────────────────────────────────────────
section("End-to-end present()")

# Realistic mini-feed: two duplicate Indeed funding stories from
# different outlets + one LinkedIn hire + one Indeed product launch.
f1 = mk(id=1001, title="Indeed raises $100m Series D",
        competitor="Indeed", signal_type="funding",
        source="techcrunch", materiality=0.9, age_days=1)
f2 = mk(id=1002, title="Indeed raises $100m in Series D round",
        competitor="Indeed", signal_type="funding",
        source="reuters", materiality=0.7, age_days=1)
f3 = mk(id=1003, title="LinkedIn hires new VP Engineering",
        competitor="LinkedIn", signal_type="new_hire",
        source="techcrunch", materiality=0.5, age_days=2)
f4 = mk(id=1004, title="Indeed launches resume assistant",
        competitor="Indeed", signal_type="product_launch",
        source="news", materiality=0.6, age_days=3)

cards = present([f1, f2, f3, f4], now=NOW)
check("two funding stories collapsed into one cluster",
      any(c.size == 2 and c.lead.id == 1001 for c in cards))
check("present() returns 3 cards (one collapsed pair + two singletons)",
      len(cards) == 3)

# Lead of the merged cluster is the higher-materiality one.
merged = [c for c in cards if c.size == 2][0]
check("merged cluster lead = higher-materiality finding",
      merged.lead.id == 1001)
check("merged cluster includes both members",
      {m.id for m in merged.members} == {1001, 1002})


# ── §5: lead_findings helper ────────────────────────────────────────
section("Template helper")

cards = present([f1, f2, f3, f4], now=NOW)
leads = lead_findings(cards)
check("lead_findings returns one Finding per card",
      len(leads) == len(cards))
check("_cluster_size stamped on leads",
      all(hasattr(l, "_cluster_size") for l in leads))
check("singleton _cluster_size == 1",
      all(getattr(l, "_cluster_size") == 1
          for l in leads if l.id in (1003, 1004)))
check("merged lead _cluster_size == 2",
      [getattr(l, "_cluster_size") for l in leads if l.id == 1001] == [2])


# ── §6: No-signal-type bucket ──────────────────────────────────────
section("NULL signal_type bucket")

# Two findings with NULL signal_type, same competitor, near-identical
# titles → should cluster via the NULL bucket.
n1 = mk(id=2001, title="Indeed makes announcement today", signal_type=None)
n2 = mk(id=2002, title="Indeed announcement today revealed", signal_type=None)
cards = cluster([n1, n2], score_fn=score_now)
check("NULL signal_type doesn't prevent same-competitor clustering",
      len(cards) == 1 and cards[0].size == 2)


# ── §7: Embedding-based clustering ─────────────────────────────────
section("Embedding cosine clustering")

# Two findings whose titles share almost nothing but whose embeddings
# point the same way → cluster via the cosine path. This is the case
# the old Jaccard-only path used to miss.
v_event = vec(axis=7, weight=1.0)  # both vectors identical → cosine 1.0
e1 = mk(id=3001, title="LinkedIn targets $450m in recruiter revenue",
        competitor="LinkedIn", signal_type="news",
        materiality=0.7, embedding=v_event)
e2 = mk(id=3002, title="Microsoft unit sets ambitious hiring-tools goal",
        competitor="LinkedIn", signal_type="strategy",
        materiality=0.6, embedding=v_event)
# Sanity: titles really would NOT cluster on Jaccard alone.
check("setup: low title overlap between the two embeddings",
      title_jaccard(e1.title, e2.title) < rcfg.CLUSTER_JACCARD_THRESHOLD,
      detail=f"jaccard={title_jaccard(e1.title, e2.title):.3f}")
cards = cluster([e1, e2], score_fn=score_now)
check("embeddings cosine ≥ threshold → cluster despite title mismatch",
      len(cards) == 1 and cards[0].size == 2)

# Two embeddings on different axes → cosine ~0 → stay separate even
# inside the same competitor bucket.
e3 = mk(id=3003, title="Some unrelated story",
        competitor="LinkedIn", embedding=vec(axis=7))
e4 = mk(id=3004, title="Yet another unrelated story",
        competitor="LinkedIn", embedding=vec(axis=42))
cards = cluster([e3, e4], score_fn=score_now)
check("orthogonal embeddings → two singleton clusters",
      len(cards) == 2 and all(c.size == 1 for c in cards))

# One side has an embedding, the other doesn't → falls back to Jaccard.
# Identical titles → Jaccard 1.0 → still cluster.
e5 = mk(id=3005, title="Indeed announces partnership with Acme",
        competitor="Indeed", embedding=vec(axis=7))
e6 = mk(id=3006, title="Indeed announces partnership with Acme",
        competitor="Indeed", embedding=None)
cards = cluster([e5, e6], score_fn=score_now)
check("missing embedding on one side → Jaccard fallback still clusters",
      len(cards) == 1 and cards[0].size == 2)

# Stale-model embedding (model field doesn't match config) → treated as
# missing, falls back to Jaccard.
e7 = mk(id=3007, title="Indeed picks new CFO",
        competitor="Indeed", embedding=vec(axis=7))
e7.embedding_model = "voyage-OLD-MODEL-NAME"
e8 = mk(id=3008, title="Indeed picks new CFO",
        competitor="Indeed", embedding=vec(axis=7))
cards = cluster([e7, e8], score_fn=score_now)
check("stale-model embedding ignored, identical titles still cluster",
      len(cards) == 1 and cards[0].size == 2)

# Cosine threshold respected — vector pair just below the bar should not
# cluster. With voyage-3-lite EMBEDDING_DIM=512 and weight=0.5 spread,
# the cosine of two `vec(axis=7, weight=0.5)` vectors is exactly 1.0
# (identical), so build a "near miss" by mixing two different axes.
mix_a = vec(axis=7, weight=1.0)
mix_b = vec(axis=8, weight=1.0)
near_miss = ((mix_a + mix_b) / np.linalg.norm(mix_a + mix_b)).astype(np.float32)
# cos(mix_a, near_miss) = 1/sqrt(2) ≈ 0.707 — below the 0.85 threshold.
e9  = mk(id=3009, title="Indeed Q1 earnings beat",
         competitor="Indeed", embedding=mix_a)
e10 = mk(id=3010, title="Indeed launches enterprise tier",
         competitor="Indeed", embedding=near_miss)
cards = cluster([e9, e10], score_fn=score_now)
check("cosine below threshold → two clusters (no false merge)",
      len(cards) == 2,
      detail=f"cos={float((mix_a * near_miss).sum()):.3f}")

# Override threshold via kwarg → can be loosened per-call.
cards = cluster([e9, e10], score_fn=score_now, cosine_threshold=0.5)
check("cosine_threshold kwarg loosens clustering at call site",
      len(cards) == 1 and cards[0].size == 2)


# ── Summary ─────────────────────────────────────────────────────────
print(f"\n{_passes} passed, {len(_failures)} failed")
if _failures:
    print("Failures:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
sys.exit(0)
