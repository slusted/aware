"""One-shot: re-classify existing rows that landed in `integration` but
are actually M&A.

Background: until the m_and_a / integration split (May 2026), the
classifier lumped acquisitions, mergers, and partnerships into a single
`integration` bucket. This script walks signal_type='integration' rows,
re-runs the regex classifier, and migrates anything that now matches
the M&A regex over to signal_type='m_and_a' with materiality=0.9.

Idempotent — re-running picks up nothing once converged. Defaults to
dry-run; pass --apply to commit.

Usage:
  py scripts/backfill_m_and_a.py            # dry-run: counts only
  py scripts/backfill_m_and_a.py --apply    # actually write
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.models import Finding
from app.signals.extract import _M_AND_A_RE


BATCH = 500


def _is_m_and_a(f: Finding) -> bool:
    title = f.title or ""
    content = (f.content or "")[:2000]
    return bool(_M_AND_A_RE.search(f"{title}\n{content}"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit changes; default is dry-run")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        total = (
            db.query(Finding)
            .filter(Finding.signal_type == "integration")
            .count()
        )
        print(f"[backfill_m_and_a] {total} integration rows to inspect")
        if total == 0:
            return 0

        last_id = 0
        scanned = 0
        migrated = 0
        while True:
            rows = (
                db.query(Finding)
                .filter(Finding.signal_type == "integration", Finding.id > last_id)
                .order_by(Finding.id)
                .limit(BATCH)
                .all()
            )
            if not rows:
                break
            for f in rows:
                last_id = f.id
                scanned += 1
                if not _is_m_and_a(f):
                    continue
                migrated += 1
                if args.apply:
                    f.signal_type = "m_and_a"
                    f.materiality = 0.9
                    payload = dict(f.payload or {})
                    payload["matched"] = "m_and_a_backfill"
                    f.payload = payload
            if args.apply:
                db.commit()
                print(f"[backfill_m_and_a] committed through id {last_id}: {migrated} migrated / {scanned} scanned")

        print(
            f"[backfill_m_and_a] done — {migrated}/{scanned} rows "
            f"{'migrated' if args.apply else 'would be migrated'} to m_and_a"
        )
        if not args.apply:
            print("[backfill_m_and_a] dry-run — re-run with --apply to write")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
