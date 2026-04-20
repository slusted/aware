"""Backfill findings.summary for rows scraped before the summarizer existed.

Usage:
    python scripts/backfill_finding_summaries.py           # all null-summary rows
    python scripts/backfill_finding_summaries.py --limit 50
    python scripts/backfill_finding_summaries.py --since 2026-04-01
    python scripts/backfill_finding_summaries.py --dry-run

Safe to re-run: only touches rows where summary IS NULL. Uses the same
Haiku helper as the live pipeline; fan-out is a small threadpool so a
few hundred rows take seconds, not minutes.
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# Let the script run from the repo root without a manual PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before the app/summarizer imports touch os.environ — the web
# process does this in app/main.py, but standalone scripts need it explicitly.
try:
    from dotenv import load_dotenv, find_dotenv
    # override=True is defensive: on some shells (PowerShell BOM/encoding
    # quirks) load_dotenv silently no-ops without it.
    load_dotenv(find_dotenv(), override=True)
except ImportError:
    pass

from app.db import SessionLocal
from app.models import Finding
from app.signals.summarize import summarize_finding


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None,
                   help="Max number of rows to process (default: all).")
    p.add_argument("--since", type=str, default=None,
                   help="Only findings created on/after this ISO date (YYYY-MM-DD).")
    p.add_argument("--workers", type=int, default=8,
                   help="Parallel Haiku calls (default: 8).")
    p.add_argument("--dry-run", action="store_true",
                   help="Summarize and print, but don't write to DB.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    db = SessionLocal()
    try:
        q = db.query(Finding).filter(Finding.summary.is_(None))
        if args.since:
            since = datetime.fromisoformat(args.since)
            q = q.filter(Finding.created_at >= since)
        q = q.order_by(Finding.created_at.desc())
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()
        if not rows:
            print("nothing to backfill — all findings have summaries")
            return 0

        print(f"backfilling {len(rows)} finding(s) with {args.workers} workers"
              f"{' (dry run)' if args.dry_run else ''}")

        def _one(row: Finding) -> tuple[int, str | None]:
            return row.id, summarize_finding(
                title=row.title,
                content=row.content,
                signal_type=row.signal_type,
                competitor=row.competitor,
            )

        written = skipped = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for fid, summary in pool.map(_one, rows):
                if not summary:
                    skipped += 1
                    print(f"  [{fid}] skipped (no usable content)")
                    continue
                if args.dry_run:
                    print(f"  [{fid}] {summary}")
                else:
                    # Re-fetch in this session; pool workers returned plain ids.
                    row = db.get(Finding, fid)
                    if row and row.summary is None:
                        row.summary = summary
                        written += 1
        if not args.dry_run:
            db.commit()
        print(f"done — wrote {written}, skipped {skipped}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
