"""Presentation layer between the ranker and the stream template
(docs/ranker/06-cluster-diversity.md).

Two jobs, in order:
  1. Cluster near-duplicate findings so one real-world event = one card.
  2. Diversify the top-of-feed via MMR so the top slots don't collapse
     into a single (competitor, signal_type) combo.

Clustering buckets by competitor only and uses embedding cosine when
both findings carry a current-model vector, falling back to title
Jaccard otherwise. The signal_type is no longer part of the bucket key:
the LLM classifier sometimes splits two reports of the same event into
different signal_types (e.g. one tagged `news`, the other `funding`),
which used to leave duplicates uncollapsed.

Pure post-processing: no DB, no LLM calls. Embedding bytes are read
straight off the Finding row via the voyage adapter's unpacker — no
network. Input is a list of Finding rows (already filtered and initially
ordered by the caller); output is a list of ClusterCard ordered for
render. Runtime-only — clusters are recomputed per request and never
persisted.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from ..adapters import voyage as _voyage
from ..models import Finding
from . import config as rcfg


# ── Output type ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClusterCard:
    """One render-ready card. `lead` is what the template renders;
    `members` is the full group (lead included at index 0). `size` is
    the sibling count — `len(members)`."""
    lead: Finding
    members: tuple[Finding, ...]
    score: float

    @property
    def size(self) -> int:
        return len(self.members)


# ── Stand-in scorer ─────────────────────────────────────────────────

def effective_date(finding: Finding) -> datetime | None:
    """Prefer the source's publish date over our fetch timestamp.
    Scrape-time can lag the real publish by years (SERP hits on old
    articles, backfills), so anything time-sensitive — recency decay,
    stream ordering, tie-breaks — must use the content's real age."""
    return finding.published_at or finding.created_at


def default_score(
    finding: Finding,
    *,
    now: datetime | None = None,
    seen_count: int = 0,
    user_centroid: object | None = None,
) -> float:
    """Materiality + recency − seen-decay (+ optional embedding match).
    Stand-in scorer until spec 03's real one lands. Deterministic; same
    input → same output.

    `seen_count` is the number of prior view/open events the user fired
    for this finding OUTSIDE the trailing exclusion window (default 60
    min, see config). Views inside the window are treated as "current
    session" and don't count — otherwise within-session scrolling would
    reshuffle the list.

    `user_centroid` is the spec-08 taste portrait — a pre-normalized
    numpy float32 array. When provided alongside a finding embedding
    that matches the current model, this adds a `EMBEDDING_WEIGHT *
    cosine` term. Either side missing → term contributes 0 silently
    so today's behaviour holds.
    """
    now = now or datetime.utcnow()
    materiality = finding.materiality or 0.0
    ref = effective_date(finding) or now
    age_days = max(0.0, (now - ref).total_seconds() / 86400.0)
    recency = rcfg.STANDIN_RECENCY_BOOST * math.exp(
        -math.log(2.0) * age_days / rcfg.STANDIN_RECENCY_HALFLIFE_DAYS
    )
    capped = min(max(seen_count, 0), rcfg.STANDIN_SEEN_DECAY_MAX_VIEWS)
    seen_penalty = rcfg.STANDIN_SEEN_DECAY_PER_VIEW * capped

    embedding_match = 0.0
    if (
        user_centroid is not None
        and finding.embedding is not None
        and finding.embedding_model == rcfg.EMBEDDING_MODEL
    ):
        finding_vec = _voyage.unpack(finding.embedding)
        if finding_vec is not None:
            # Both vectors L2-normalized at write time → cosine = dot.
            try:
                cos = float((user_centroid * finding_vec).sum())  # type: ignore[operator]
            except Exception:
                cos = 0.0
            embedding_match = rcfg.EMBEDDING_WEIGHT * cos

    return materiality + recency - seen_penalty + embedding_match


# ── Title normalization + Jaccard ───────────────────────────────────

_WORD_RE = re.compile(r"[a-z0-9]+")


def _title_tokens(title: str | None) -> frozenset[str]:
    """Lowercase → word tokens → drop stopwords and 1-char fragments.
    Empty/None title yields an empty set (clusters of 1)."""
    if not title:
        return frozenset()
    lowered = title.lower()
    toks = _WORD_RE.findall(lowered)
    return frozenset(t for t in toks if len(t) >= 2 and t not in rcfg.CLUSTER_STOPWORDS)


def title_jaccard(a: str | None, b: str | None) -> float:
    """Set Jaccard on normalized tokens. 0.0 when either side is empty
    (no title → never cluster)."""
    sa = _title_tokens(a)
    sb = _title_tokens(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


# ── Clustering ──────────────────────────────────────────────────────

def _bucket_key(finding: Finding) -> str:
    """Bucket by normalized competitor only. Different competitors never
    cluster; everything else is decided by similarity inside the bucket.
    Dropping signal_type fixes the case where the same event was split
    across two signal_types by the classifier and never collapsed."""
    return (finding.competitor or "").strip().lower()


def _finding_embedding(finding: Finding):
    """Unpack a Finding's embedding when it matches the current model.
    Returns a numpy array (L2-normalized at write time → cosine = dot)
    or None when the column is empty or the row predates the active
    model — in either case the caller falls back to title Jaccard."""
    if finding.embedding is None:
        return None
    if finding.embedding_model != rcfg.EMBEDDING_MODEL:
        return None
    return _voyage.unpack(finding.embedding)


def _are_duplicates(
    a: Finding,
    b: Finding,
    *,
    a_emb,
    b_emb,
    cosine_threshold: float,
    jaccard_threshold: float,
) -> bool:
    """Embedding cosine when both vectors are available (the norm now
    that the backfill has run); title Jaccard fallback when either side
    is missing a vector — so a Voyage outage or pre-embedding row can't
    silently turn dedup off."""
    if a_emb is not None and b_emb is not None:
        try:
            cos = float((a_emb * b_emb).sum())
        except Exception:
            cos = 0.0
        return cos >= cosine_threshold
    return title_jaccard(a.title, b.title) >= jaccard_threshold


class _UnionFind:
    """Minimal union-find keyed by int index."""

    __slots__ = ("parent",)

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        # Path compression — keeps find() ~O(α(n)).
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def cluster(
    findings: list[Finding],
    *,
    score_fn: Callable[[Finding], float],
    jaccard_threshold: float | None = None,
    cosine_threshold: float | None = None,
) -> list[ClusterCard]:
    """Group near-duplicates within each competitor bucket. Within a
    bucket each pair is compared via embedding cosine when both vectors
    are present, falling back to title Jaccard when either side lacks
    one. Returns clusters sorted by lead score desc, ties broken by
    lead effective_date desc."""
    j_thresh = jaccard_threshold if jaccard_threshold is not None else rcfg.CLUSTER_JACCARD_THRESHOLD
    c_thresh = cosine_threshold if cosine_threshold is not None else rcfg.CLUSTER_COSINE_THRESHOLD

    if not findings:
        return []

    scores = [score_fn(f) for f in findings]
    # Unpack each embedding once up-front; pairwise loop just dots them.
    embeddings = [_finding_embedding(f) for f in findings]

    # Bucket findings by competitor so pairwise comparison is only
    # computed within plausible candidate sets.
    buckets: dict[str, list[int]] = {}
    for idx, f in enumerate(findings):
        buckets.setdefault(_bucket_key(f), []).append(idx)

    uf = _UnionFind(len(findings))
    for members in buckets.values():
        if len(members) < 2:
            continue
        # Pairwise within the bucket. Buckets stay small in practice —
        # a single competitor in a 30-day window is rarely more than a
        # few dozen items, so O(n²) cosine dots are negligible.
        for i_local, i in enumerate(members):
            for j in members[i_local + 1:]:
                if _are_duplicates(
                    findings[i], findings[j],
                    a_emb=embeddings[i], b_emb=embeddings[j],
                    cosine_threshold=c_thresh,
                    jaccard_threshold=j_thresh,
                ):
                    uf.union(i, j)

    # Group indices by root → cluster membership list.
    groups: dict[int, list[int]] = {}
    for idx in range(len(findings)):
        groups.setdefault(uf.find(idx), []).append(idx)

    cards: list[ClusterCard] = []
    for member_idxs in groups.values():
        # Lead = highest score, tie-break on newest effective date.
        member_idxs.sort(
            key=lambda i: (
                -scores[i],
                -(effective_date(findings[i]).timestamp() if effective_date(findings[i]) else 0.0),
            )
        )
        members = tuple(findings[i] for i in member_idxs)
        lead_score = scores[member_idxs[0]]
        cards.append(ClusterCard(lead=members[0], members=members, score=lead_score))

    cards.sort(
        key=lambda c: (
            -c.score,
            -(effective_date(c.lead).timestamp() if effective_date(c.lead) else 0.0),
        )
    )
    return cards


# ── Diversity — MMR ─────────────────────────────────────────────────

def _cluster_similarity(a: ClusterCard, b: ClusterCard) -> float:
    """Categorical overlap similarity in [0, 1]. Sum of per-dimension
    contributions for dimensions where both leads share a non-empty
    value. Capped at 1.0."""
    la, lb = a.lead, b.lead
    sim = 0.0
    for dim, weight in rcfg.MMR_SIM_WEIGHTS.items():
        attr = {
            "competitor": "competitor",
            "signal_type": "signal_type",
            "topic": "topic",
            "source": "source",
        }[dim]
        va = getattr(la, attr, None)
        vb = getattr(lb, attr, None)
        if va is None or vb is None:
            continue
        # Normalize strings so "Indeed" / "indeed " collapse to equal.
        if isinstance(va, str):
            va = va.strip().lower()
        if isinstance(vb, str):
            vb = vb.strip().lower()
        if va == "" or vb == "":
            continue
        if va == vb:
            sim += weight
    return min(1.0, sim)


def diversify(
    clusters: list[ClusterCard],
    *,
    window: int | None = None,
    lambda_: float | None = None,
) -> list[ClusterCard]:
    """Greedy MMR on the top `window` cluster-leads. Everything beyond
    the window keeps the input order unchanged.

    Tail clusters (beyond window) are appended in their original order,
    preserving score-desc intuition outside the attention zone.
    """
    w = window if window is not None else rcfg.MMR_WINDOW
    lam = lambda_ if lambda_ is not None else rcfg.MMR_LAMBDA

    if not clusters or w <= 0:
        return list(clusters)

    head = clusters[:w]
    tail = clusters[w:]

    if len(head) <= 1:
        return head + tail

    remaining = list(head)
    picked: list[ClusterCard] = []

    # Seed: pick the highest-scoring cluster first. After that, each
    # pick maximizes λ·score − (1−λ)·max_similarity_to_picked.
    remaining.sort(
        key=lambda c: (
            -c.score,
            -(effective_date(c.lead).timestamp() if effective_date(c.lead) else 0.0),
        )
    )
    picked.append(remaining.pop(0))

    while remaining:
        best_idx = 0
        best_mmr = -float("inf")
        for idx, cand in enumerate(remaining):
            max_sim = max(_cluster_similarity(cand, p) for p in picked)
            mmr = lam * cand.score - (1.0 - lam) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = idx
        picked.append(remaining.pop(best_idx))

    return picked + tail


# ── Public entry point ─────────────────────────────────────────────

def present(
    findings: list[Finding],
    *,
    score_fn: Callable[[Finding], float] | None = None,
    now: datetime | None = None,
    seen_count_by_id: dict[int, int] | None = None,
    user_centroid: object | None = None,
    mmr_window: int | None = None,
    mmr_lambda: float | None = None,
    jaccard_threshold: float | None = None,
    cosine_threshold: float | None = None,
) -> list[ClusterCard]:
    """Cluster near-duplicates, then diversify the top of the list.

    `score_fn` defaults to `default_score` — materiality + recency − seen-decay
    + optional embedding match (spec 08). Once spec 03's ranker lands, callers
    pass a lookup into the Scored map.

    `seen_count_by_id` maps finding.id → number of prior view/open events
    outside the exclusion window (spec 07). Threaded into the default
    scorer only; when a caller supplies its own `score_fn`, this param is
    ignored — the custom scorer owns its math.

    `user_centroid` is the spec-08 taste portrait (numpy float32, pre-normalized).
    Threaded into the default scorer only. Callers without a profile pass None
    and the embedding term silently contributes 0.
    """
    if not findings:
        return []

    _now = now or datetime.utcnow()

    if score_fn is None:
        seen_map = seen_count_by_id or {}
        _centroid = user_centroid

        def _score(f: Finding, _now: datetime = _now) -> float:
            return default_score(
                f,
                now=_now,
                seen_count=seen_map.get(f.id, 0),
                user_centroid=_centroid,
            )
        score_fn = _score

    clustered = cluster(
        findings,
        score_fn=score_fn,
        jaccard_threshold=jaccard_threshold,
        cosine_threshold=cosine_threshold,
    )
    return diversify(clustered, window=mmr_window, lambda_=mmr_lambda)


# ── Flattening helper (template convenience) ───────────────────────

def lead_findings(cards: Iterable[ClusterCard]) -> list[Finding]:
    """Return the list of lead Findings, each stamped with a runtime
    `_cluster_size` attribute so the template can render the +N chip
    without restructuring the existing loop.
    """
    out: list[Finding] = []
    for card in cards:
        lead = card.lead
        # Runtime attribute — SQLAlchemy instances tolerate arbitrary
        # setattr. Never written back to the DB (no flush happens in
        # read-only request paths).
        lead._cluster_size = card.size  # type: ignore[attr-defined]
        out.append(lead)
    return out
