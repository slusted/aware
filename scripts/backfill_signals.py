"""One-shot: classify every Finding row whose signal_type is NULL.

Idempotent — only touches unclassified rows. Re-running after new findings
arrive without signal_type (e.g. from a legacy write path that slipped
through) will pick them up too.

Usage:
  python scripts/backfill_signals.py            # dry-run: counts only
  python scripts/backfill_signals.py --apply    # actually write
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.models import Finding
from app.signals.extract import classify


BATCH = 500


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit changes; default is dry-run")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        total = db.query(Finding).filter(Finding.signal_type.is_(None)).count()
        print(f"[backfill] {total} findings to classify")
        if total == 0:
            return 0

        counts: Counter = Counter()
        processed = 0
        offset = 0
        while True:
            rows = (
                db.query(Finding)
                .filter(Finding.signal_type.is_(None))
                .order_by(Finding.id)
                .limit(BATCH)
                .all()
            )
            if not rows:
                break
            for f in rows:
                st, mat, payload = classify({
                    "topic": f.topic,
                    "source": f.source,
                    "title": f.title,
                    "content": f.content,
                })
                counts[st] += 1
                if args.apply:
                    f.signal_type = st
                    f.materiality = mat
                    f.payload = payload
            processed += len(rows)
            if args.apply:
                db.commit()
                print(f"[backfill] committed {processed}/{total}")
            else:
                # dry-run: reset the session so the next batch isn't the same rows
                db.expire_all()
                offset += len(rows)
                if offset >= total:
                    break

        print("[backfill] distribution:")
        for st, n in counts.most_common():
            pct = n / processed * 100
            print(f"    {st:18} {n:6}  ({pct:.1f}%)")

        if not args.apply:
            print("[backfill] dry-run complete — re-run with --apply to write")
        else:
            print(f"[backfill] done — {processed} rows updated")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
