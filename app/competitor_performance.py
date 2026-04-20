"""Per-competitor performance report for the history-aware Optimise button.

Aggregates recent Finding rows into a plain-text block that gets injected
into the autofill agent's system prompt. The agent then tunes keywords /
subreddits / domains based on evidence: prune what's producing nothing
or only low-materiality noise, keep what's working, propose replacements
for the dead weight.

Pure SQL + string formatting — no LLM, no external I/O. Returns an
empty string when there's nothing useful to say (new competitor, no
findings yet), so callers can branch trivially.

Kept separate from competitor_autofill.py because the aggregation will
grow (more signal types, trend deltas) and this layer has no Anthropic
SDK dependency — easier to test and reuse (e.g. for a /settings page
showing per-competitor health).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Competitor, Finding


# Materiality cutoffs. Matches the rubric in app/signals/llm_classify.py
# so the report's "material vs noise" buckets line up with how findings
# are scored in the first place.
_MATERIAL_THRESHOLD = 0.6
_NOISE_THRESHOLD = 0.3


def _fmt_avg(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "—"


def _split_keyword_entries(configured: list[str]) -> list[str]:
    """Some legacy competitor rows store keywords as a single string with
    embedded newlines instead of a proper list. Split those out so the
    silent/producing compare is done against what the scanner would have
    actually iterated over (after its own split-on-whitespace). De-dup
    while preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for entry in configured or []:
        for piece in str(entry).splitlines():
            p = piece.strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _keyword_block(db: Session, competitor: Competitor, since: datetime,
                   configured: list[str]) -> list[str]:
    """Group findings by matched_keyword over the window and score each
    configured keyword on three signals:

    1. Volume (count of findings)
    2. Digest inclusion — the analyzer's per-finding threat labels
       (HIGH/MEDIUM/LOW/NOISE). HIGH and MEDIUM are the honest "useful
       intel" signals; NOISE is the explicit "don't-bother" signal.
       Findings that weren't referenced at all stay neutral.
    3. Signal-type diversity — a keyword producing a mix of
       product_launch / funding / new_hire is earning its keep; one
       producing only 'news' is probably too generic.

    Materiality stays in the output as an additional signal but doesn't
    drive the verdict — the user flagged that as a weak proxy. Keywords
    with findings but not currently configured are ignored (pruning
    them is the whole point of Optimise).
    """
    # Pull per-keyword aggregate stats and per-finding detail in two passes.
    # Row volume is bounded by keywords × window so Python-side aggregation
    # is fine — keeps the SQL simple and portable across sqlite/postgres.
    rows = (
        db.query(
            Finding.matched_keyword,
            func.count(Finding.id).label("n"),
            func.avg(Finding.materiality).label("avg_mat"),
        )
        .filter(
            Finding.competitor == competitor.name,
            Finding.created_at >= since,
            Finding.matched_keyword.isnot(None),
        )
        .group_by(Finding.matched_keyword)
        .all()
    )
    detail = (
        db.query(
            Finding.matched_keyword,
            Finding.materiality,
            Finding.signal_type,
            Finding.digest_threat_level,
        )
        .filter(
            Finding.competitor == competitor.name,
            Finding.created_at >= since,
            Finding.matched_keyword.isnot(None),
        )
        .all()
    )
    hi_mat: dict[str, int] = {}
    lo_mat: dict[str, int] = {}
    threat_counts: dict[str, dict[str, int]] = {}  # kw -> {HIGH, MEDIUM, LOW, NOISE}
    signal_types: dict[str, set[str]] = {}
    for kw, mat, st, lvl in detail:
        if mat is not None:
            if mat >= _MATERIAL_THRESHOLD:
                hi_mat[kw] = hi_mat.get(kw, 0) + 1
            elif mat < _NOISE_THRESHOLD:
                lo_mat[kw] = lo_mat.get(kw, 0) + 1
        if lvl in ("HIGH", "MEDIUM", "LOW", "NOISE"):
            bucket = threat_counts.setdefault(kw, {})
            bucket[lvl] = bucket.get(lvl, 0) + 1
        if st:
            signal_types.setdefault(kw, set()).add(st)

    per_kw: dict[str, dict] = {}
    for kw, n, avg in rows:
        tc = threat_counts.get(kw, {})
        per_kw[kw] = {
            "n": int(n),
            "avg": float(avg) if avg is not None else None,
            "hi_mat": hi_mat.get(kw, 0),
            "lo_mat": lo_mat.get(kw, 0),
            "high": tc.get("HIGH", 0),
            "medium": tc.get("MEDIUM", 0),
            "low": tc.get("LOW", 0),
            "noise": tc.get("NOISE", 0),
            "diversity": len(signal_types.get(kw, set())),
        }

    lines: list[str] = ["## Keyword productivity (matched_keyword attribution)"]
    lines.append(
        "  Verdict reads: STRONG = repeatedly featured as HIGH/MEDIUM in digests or "
        "produces diverse signal types. WEAK = mostly labelled NOISE or pure 'news' "
        "churn. OK = somewhere in between. UNSCORED = no digest has referenced this "
        "keyword's findings yet (common for new keywords — revisit after next digest)."
    )
    if not configured:
        lines.append("  (no keywords configured — agent should propose an initial set)")
        return lines

    producing, silent = [], []
    for kw in configured:
        stats = per_kw.get(kw)
        if stats and stats["n"] > 0:
            producing.append((kw, stats))
        else:
            silent.append(kw)

    # Rank: HIGH-threat hits first, then MEDIUM, then materiality — so the
    # most useful keywords sit at the top of the block where the agent's
    # attention lands first.
    producing.sort(key=lambda x: (
        -x[1]["high"], -x[1]["medium"], -(x[1]["avg"] or 0),
    ))
    for kw, s in producing:
        total_labeled = s["high"] + s["medium"] + s["low"] + s["noise"]
        if total_labeled == 0:
            verdict = "UNSCORED"
        elif s["high"] >= 1 or s["medium"] >= 2 or s["diversity"] >= 3:
            verdict = "STRONG"
        elif s["noise"] >= max(1, total_labeled // 2) or (s["diversity"] <= 1 and s["low"] >= 2):
            verdict = "WEAK"
        else:
            verdict = "OK"
        lines.append(
            f"  - {kw!r}: {s['n']} hits · "
            f"digest HIGH={s['high']} MED={s['medium']} LOW={s['low']} NOISE={s['noise']} · "
            f"signal-types={s['diversity']} · "
            f"avg-materiality={_fmt_avg(s['avg'])}  → {verdict}"
        )
    if silent:
        lines.append(f"  Silent (0 hits — strong prune candidates): {silent}")
    return lines


def _source_block(db: Session, competitor: Competitor, since: datetime) -> list[str]:
    rows = (
        db.query(
            Finding.source,
            func.count(Finding.id).label("n"),
            func.avg(Finding.materiality).label("avg_mat"),
        )
        .filter(
            Finding.competitor == competitor.name,
            Finding.created_at >= since,
        )
        .group_by(Finding.source)
        .order_by(func.count(Finding.id).desc())
        .all()
    )
    if not rows:
        return []
    lines = ["## Source productivity (where findings came from)"]
    for source, n, avg in rows:
        lines.append(f"  - {source or '(unknown)'}: {int(n)} findings, avg materiality {_fmt_avg(float(avg) if avg is not None else None)}")
    return lines


def _normalize_sub(s: str) -> str:
    """Reduce any subreddit reference — 'r/jobs', 'reddit/r/jobs',
    'reddit/jobs', '/r/jobs', 'jobs' — to the bare name lowercased."""
    s = (s or "").strip().lower()
    # Pull off the last slash-delimited segment so both 'reddit/r/jobs'
    # and 'reddit/jobs' collapse to the final piece.
    s = s.rsplit("/", 1)[-1]
    if s.startswith("r/"):
        s = s[2:]
    return s


def _subreddit_block(db: Session, competitor: Competitor, since: datetime,
                     configured: list[str]) -> list[str]:
    """Configured subreddits × actual hits. Sources from reddit look like
    'reddit/r/jobs' or 'reddit/jobs' depending on the adapter — normalize
    both sides so the lookup doesn't silently miss."""
    if not configured:
        return []
    rows = (
        db.query(
            Finding.source,
            func.count(Finding.id).label("n"),
            func.avg(Finding.materiality).label("avg_mat"),
        )
        .filter(
            Finding.competitor == competitor.name,
            Finding.created_at >= since,
            Finding.source.like("reddit/%"),
        )
        .group_by(Finding.source)
        .all()
    )
    hits: dict[str, tuple[int, float | None]] = {}
    for source, n, avg in rows:
        key = _normalize_sub(source or "")
        if not key:
            continue
        # Aggregate in case two source strings normalize to the same sub.
        prev_n, prev_avg = hits.get(key, (0, None))
        new_n = prev_n + int(n)
        new_avg = float(avg) if avg is not None else prev_avg
        hits[key] = (new_n, new_avg)

    producing, silent = [], []
    for sub in configured:
        key = _normalize_sub(sub)
        got = hits.get(key)
        if got and got[0] > 0:
            producing.append((sub, got))
        else:
            silent.append(sub)

    lines = ["## Subreddit productivity"]
    producing.sort(key=lambda x: -x[1][0])
    for sub, (n, avg) in producing:
        lines.append(f"  - r/{_normalize_sub(sub)}: {n} hits, avg materiality {_fmt_avg(avg)}")
    if silent:
        lines.append(f"  Silent (0 hits — drop candidates): {silent}")
    return lines


def _overview_block(db: Session, competitor: Competitor, since: datetime) -> list[str]:
    total = (
        db.query(func.count(Finding.id))
        .filter(
            Finding.competitor == competitor.name,
            Finding.created_at >= since,
        )
        .scalar()
    ) or 0
    if total == 0:
        return []
    rows = (
        db.query(Finding.materiality, Finding.digest_threat_level)
        .filter(
            Finding.competitor == competitor.name,
            Finding.created_at >= since,
        )
        .all()
    )
    mat_hi = sum(1 for m, _ in rows if m is not None and m >= _MATERIAL_THRESHOLD)
    mat_lo = sum(1 for m, _ in rows if m is not None and m < _NOISE_THRESHOLD)
    mat_mid = total - mat_hi - mat_lo
    dig_high = sum(1 for _, l in rows if l == "HIGH")
    dig_med = sum(1 for _, l in rows if l == "MEDIUM")
    dig_low = sum(1 for _, l in rows if l == "LOW")
    dig_noise = sum(1 for _, l in rows if l == "NOISE")
    dig_total = dig_high + dig_med + dig_low + dig_noise
    return [
        f"## Overview — {total} findings in window",
        f"  Materiality (classifier score): material (≥{_MATERIAL_THRESHOLD}): {mat_hi} · "
        f"routine: {mat_mid} · noise (<{_NOISE_THRESHOLD}): {mat_lo}",
        f"  Digest inclusion (analyst label): HIGH: {dig_high} · MEDIUM: {dig_med} · "
        f"LOW: {dig_low} · NOISE: {dig_noise} · unlabeled: {total - dig_total}",
    ]


def build_performance_report(db: Session, competitor_id: int, days: int = 60) -> str:
    """Compose a human-readable performance report for one competitor.

    Returns an empty string when the competitor has no findings in the
    window — callers can skip the prompt-injection block entirely.
    """
    c = db.get(Competitor, competitor_id)
    if not c:
        return ""
    since = datetime.utcnow() - timedelta(days=days)

    sections: list[list[str]] = [
        _overview_block(db, c, since),
        _source_block(db, c, since),
        _keyword_block(db, c, since, _split_keyword_entries(list(c.keywords or []))),
        _subreddit_block(db, c, since, list(c.subreddits or [])),
    ]
    # Drop empty sections; if everything's empty the window had zero data.
    sections = [s for s in sections if s]
    if not sections:
        return ""

    header = (
        f"# Performance report for '{c.name}' — last {days} days\n"
        f"Use this to decide which keywords/subreddits to keep, prune, or replace. "
        f"Prefer evidence over guesswork: if a term is silent or noisy, replace it "
        f"with something you can verify via search_web before adding.\n"
    )
    return header + "\n".join("\n".join(section) for section in sections)
