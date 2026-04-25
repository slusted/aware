"""Backfill findings.embedding for rows scraped before spec 08 shipped.

Usage:
    python scripts/backfill_embeddings.py             # all null-embedding rows
    python scripts/backfill_embeddings.py --limit 200
    python scripts/backfill_embeddings.py --since 2026-04-01
    python scripts/backfill_embeddings.py --batch 64
    python scripts/backfill_embeddings.py --dry-run

Safe to re-run: only touches rows where embedding IS NULL or where
embedding_model doesn't match the current EMBEDDING_MODEL (latter case
covers a model bump). Newer rows are processed first so the next rollup
gets the freshest signal even if the script is interrupted.

Reuses app.signals.embed.embed_finding_text — same recipe as the live
extractor. Voyage's batch endpoint gets up to 64 texts per call.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

# Let the script run from the repo root without a manual PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(), override=True)
except ImportError:
    pass

from sqlalchemy import or_

from app.adapters import voyage as _voyage
from app.db import SessionLocal
from app.models import Finding
from app.ranker import config as rcfg


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None,
                   help="Max rows to process (default: all eligible).")
    p.add_argument("--since", type=str, default=None,
                   help="Only findings created on/after this ISO date (YYYY-MM-DD).")
    p.add_argument("--batch", type=int, default=64,
                   help="Embeddings per Voyage call (default: 64; max 128).")
    p.add_argument("--dry-run", action="store_true",
                   help="Embed but don't write — useful for cost/latency probes.")
    return p.parse_args()


def _build_text(f: Finding) -> str:
    """Same recipe as app/signals/embed.py — title + summary/content."""
    title_part = (f.title or "").strip()
    body_part = (f.summary or f.content or "").strip()
    if not title_part and not body_part:
        return ""
    return (title_part + "\n\n" + body_part).strip()[: rcfg.EMBEDDING_INPUT_CHAR_CAP]


def main() -> int:
    args = parse_args()
    if args.batch < 1 or args.batch > 128:
        print(f"--batch must be between 1 and 128, got {args.batch}", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        q = db.query(Finding).filter(
            or_(
                Finding.embedding.is_(None),
                Finding.embedding_model != rcfg.EMBEDDING_MODEL,
            )
        )
        if args.since:
            since = datetime.fromisoformat(args.since)
            q = q.filter(Finding.created_at >= since)
        q = q.order_by(Finding.created_at.desc())
        if args.limit is not None:
            q = q.limit(args.limit)

        rows = q.all()
        if not rows:
            print("[backfill] nothing to do — all findings already embedded")
            return 0

        print(f"[backfill] {len(rows)} findings to embed "
              f"(model={rcfg.EMBEDDING_MODEL}, batch={args.batch})")

        processed = 0
        succeeded = 0
        skipped_empty = 0
        failed = 0

        for start in range(0, len(rows), args.batch):
            chunk = rows[start : start + args.batch]
            texts = [_build_text(f) for f in chunk]
            non_empty_idx = [i for i, t in enumerate(texts) if t]
            skipped_empty += len(chunk) - len(non_empty_idx)
            if not non_empty_idx:
                processed += len(chunk)
                continue

            sendable = [texts[i] for i in non_empty_idx]
            vectors = _voyage.embed_documents(sendable)

            for vec, slot in zip(vectors, non_empty_idx):
                f = chunk[slot]
                if vec is None:
                    failed += 1
                    continue
                if not args.dry_run:
                    f.embedding = _voyage.pack(vec)
                    f.embedding_model = rcfg.EMBEDDING_MODEL
                succeeded += 1

            if not args.dry_run:
                db.commit()
            processed += len(chunk)
            print(f"[backfill] {processed}/{len(rows)} processed "
                  f"(ok={succeeded} fail={failed} empty={skipped_empty})")

        print(f"[backfill] done — ok={succeeded} fail={failed} "
              f"empty={skipped_empty} total={processed}")
        return 0 if failed == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
