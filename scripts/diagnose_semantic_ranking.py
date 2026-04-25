"""Operational diagnostic for spec 08.

Hits the *live* DB (whatever DATABASE_URL points at — usually .env) and
answers one question: is the semantic-ranking term actually moving the
stream order for this user, and by how much?

Usage:
    python scripts/diagnose_semantic_ranking.py --email you@example.com
    python scripts/diagnose_semantic_ranking.py --user-id 1 --window-days 30
    python scripts/diagnose_semantic_ranking.py --email you@... --top 30

Three sections:

  1. Centroid status — exists? when computed? how many contributions?
  2. Embedding coverage — what fraction of recent findings have a
     current-model embedding? (None means the term silently no-ops
     for those cards regardless of centroid.)
  3. Rank diff — re-scores recent findings WITH and WITHOUT the
     centroid; prints the top-N alongside, plus per-card delta.

If section 3 shows zero movement, semantic ranking isn't influencing
the stream right now — usually because (a) no centroid yet, (b) too
few embeddings on recent findings, or (c) the centroid is orthogonal
to most current content.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(), override=True)
except ImportError:
    pass

from sqlalchemy import func  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Finding, User, UserSignalEvent  # noqa: E402
from app.ranker import config as rcfg  # noqa: E402
from app.ranker.preferences import load_profile  # noqa: E402
from app.ranker.present import default_score  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--email", help="User email")
    g.add_argument("--user-id", type=int, help="User id")
    p.add_argument("--window-days", type=int, default=30,
                   help="Score findings created within this many days (default: 30).")
    p.add_argument("--top", type=int, default=20,
                   help="How many top items to print side-by-side (default: 20).")
    p.add_argument("--limit", type=int, default=500,
                   help="Max findings to score, mirrors STREAM_SAFETY_CAP (default: 500).")
    return p.parse_args()


def fmt_score(s: float) -> str:
    return f"{s:+.3f}"


def fmt_arrow(delta_rank: int) -> str:
    if delta_rank == 0:
        return " ="
    if delta_rank > 0:
        return f"+{delta_rank}"
    return str(delta_rank)


def safe(s: str) -> str:
    """Strip characters the local console encoding can't render. Some titles
    contain emoji (e.g. ocean wave) that crash Windows cp1252 stdout."""
    enc = sys.stdout.encoding or "utf-8"
    return s.encode(enc, errors="replace").decode(enc, errors="replace")


def main() -> int:
    args = parse_args()
    db = SessionLocal()
    try:
        if args.user_id is not None:
            user = db.get(User, args.user_id)
        else:
            user = db.query(User).filter(User.email == args.email).first()
        if user is None:
            print(f"No such user.", file=sys.stderr)
            return 2
        print(f"User: id={user.id} email={user.email}")
        print(f"Current EMBEDDING_MODEL: {rcfg.EMBEDDING_MODEL} "
              f"(dim={rcfg.EMBEDDING_DIM}, weight={rcfg.EMBEDDING_WEIGHT})")
        print()

        # ── §1 Centroid status ─────────────────────────────────────
        print("=== Centroid status ===")
        profile = load_profile(db, user.id)
        if profile.taste_embedding is None:
            print("  centroid: NOT SET")
            print("  (either no rollup has run, no engaged finding has an")
            print("   embedding yet, or the stored centroid was built with")
            print("   a model that no longer matches EMBEDDING_MODEL)")
            print("  -> embedding_match contributes 0 for every card right now")
        else:
            print(f"  centroid: present ({rcfg.EMBEDDING_DIM}-dim float32)")
        print(f"  cold_start: {profile.cold_start}")
        print(f"  event_count_30d: {profile.event_count_30d}")
        print(f"  last_computed_at: {profile.last_computed_at}")
        print()

        # ── §2 Embedding coverage on the candidate set ─────────────
        print("=== Embedding coverage on recent findings ===")
        cutoff = datetime.utcnow() - timedelta(days=args.window_days)
        total = (
            db.query(func.count(Finding.id))
            .filter(Finding.created_at >= cutoff)
            .scalar() or 0
        )
        embedded = (
            db.query(func.count(Finding.id))
            .filter(
                Finding.created_at >= cutoff,
                Finding.embedding.isnot(None),
                Finding.embedding_model == rcfg.EMBEDDING_MODEL,
            )
            .scalar() or 0
        )
        pct = (100.0 * embedded / total) if total else 0.0
        print(f"  window: last {args.window_days}d")
        print(f"  total findings: {total}")
        print(f"  with current-model embedding: {embedded} ({pct:.1f}%)")
        if total and embedded == 0:
            print("  -> no card in this window can pick up an embedding term")
            print("     (run `python scripts/backfill_embeddings.py` if older)")
        print()

        # ── §3 Rank diff ───────────────────────────────────────────
        print("=== Rank diff (with centroid vs without) ===")
        candidates = (
            db.query(Finding)
            .filter(Finding.created_at >= cutoff)
            .order_by(func.coalesce(Finding.published_at, Finding.created_at).desc())
            .limit(args.limit)
            .all()
        )
        if not candidates:
            print("  no findings in window — nothing to score.")
            return 0

        now = datetime.utcnow()
        # Mirror /stream's seen_count map for parity. Reasonable approximation:
        # count view/open events outside the trailing exclusion window.
        seen_cutoff = now - timedelta(
            minutes=rcfg.STANDIN_SEEN_DECAY_EXCLUDE_MINUTES
        )
        ids = [f.id for f in candidates]
        rows = (
            db.query(
                UserSignalEvent.finding_id,
                func.count(UserSignalEvent.id),
            )
            .filter(
                UserSignalEvent.user_id == user.id,
                UserSignalEvent.event_type.in_(("view", "open")),
                UserSignalEvent.finding_id.in_(ids),
                UserSignalEvent.ts < seen_cutoff,
            )
            .group_by(UserSignalEvent.finding_id)
            .all()
        )
        seen_map = {fid: int(n) for fid, n in rows if fid is not None}

        scored = []
        for f in candidates:
            seen = seen_map.get(f.id, 0)
            s_off = default_score(f, now=now, seen_count=seen, user_centroid=None)
            s_on = default_score(f, now=now, seen_count=seen,
                                 user_centroid=profile.taste_embedding)
            scored.append((f, s_off, s_on))

        # Stable sort: score desc, then effective date desc, then id desc.
        def _key(triple, score_idx):
            f, s_off, s_on = triple
            score = (s_off, s_on)[score_idx]
            ts = (f.published_at or f.created_at or now).timestamp()
            return (-score, -ts, -f.id)

        order_off = sorted(scored, key=lambda t: _key(t, 0))
        order_on = sorted(scored, key=lambda t: _key(t, 1))

        rank_off_by_id = {t[0].id: i for i, t in enumerate(order_off)}
        rank_on_by_id = {t[0].id: i for i, t in enumerate(order_on)}

        # Aggregate: how many cards moved at all? Top-N composition diff?
        moved = sum(
            1 for fid in rank_off_by_id
            if rank_off_by_id[fid] != rank_on_by_id[fid]
        )
        top_n = args.top
        top_off_ids = {t[0].id for t in order_off[:top_n]}
        top_on_ids = {t[0].id for t in order_on[:top_n]}
        added = top_on_ids - top_off_ids
        dropped = top_off_ids - top_on_ids

        # Largest single delta (signed contribution to score, not rank).
        deltas = sorted(
            ((t[2] - t[1], t[0]) for t in scored),
            key=lambda x: abs(x[0]),
            reverse=True,
        )
        max_delta, max_finding = deltas[0] if deltas else (0.0, None)

        print(f"  candidates scored: {len(scored)}")
        print(f"  cards whose rank changed: {moved} / {len(scored)}")
        print(f"  top-{top_n} composition: {len(added)} added, {len(dropped)} dropped")
        print(f"  largest score delta: {max_delta:+.4f}"
              + (f"  ({max_finding.competitor!r}: {safe((max_finding.title or '')[:60])!r})"
                 if max_finding else ""))
        print()

        # Side-by-side top-N. Show: rank_on, score_on, delta, rank_off, title.
        print(f"  Top {top_n} WITH centroid (column shows rank shift vs without):")
        print(f"    {'rank':>4}  {'score':>7}  {'delta':>7}  {'shift':>5}  finding")
        for i, (f, s_off, s_on) in enumerate(order_on[:top_n]):
            shift = rank_off_by_id[f.id] - i  # +ve = climbed; -ve = dropped
            title = safe((f.title or "(no title)")[:80])
            comp = safe((f.competitor or "")[:14])
            print(f"    {i+1:>4}  {fmt_score(s_on):>7}  "
                  f"{fmt_score(s_on - s_off):>7}  {fmt_arrow(shift):>5}  "
                  f"{comp:14}  {title}")

        print()
        if moved == 0:
            print("VERDICT: semantic ranking is NOT influencing the stream right now.")
            if profile.taste_embedding is None:
                print("         Reason: no centroid (see §1).")
            elif embedded == 0:
                print("         Reason: no current-model embeddings on recent findings (see §2).")
            else:
                print("         Reason: embedding term is contributing but not enough to")
                print("                 reorder anything against the other components.")
        else:
            print(f"VERDICT: semantic ranking is INFLUENCING the stream — {moved} cards moved, "
                  f"{len(added)} of the top {top_n} swapped in.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
