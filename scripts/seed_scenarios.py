"""CLI wrapper around app.scenarios.seed_loader.

The actual upsert orchestration lives in
`app/scenarios/seed_loader.py` so the web process can call it from
`lifespan` (where Railway's persistent volume is guaranteed mounted).
This script keeps a thin CLI on top so manual runs still work:

    python -m scripts.seed_scenarios
    python -m scripts.seed_scenarios --json path/to/custom_seed.json

After upsert, runs app/scenarios/integrity.validate_seed and exits
non-zero on any violation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make `app.*` importable when run as a module from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.scenarios.integrity import validate_seed  # noqa: E402
from app.scenarios.seed_loader import DEFAULT_SEED_PATH, seed_payload  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--json", default=str(DEFAULT_SEED_PATH),
        help=f"Seed JSON path (default: {DEFAULT_SEED_PATH})",
    )
    ap.add_argument(
        "--skip-validate", action="store_true",
        help="Skip integrity.validate_seed after upsert (debug only).",
    )
    args = ap.parse_args()

    payload_path = Path(args.json)
    if not payload_path.is_file():
        print(f"ERROR: seed file not found: {payload_path}", file=sys.stderr)
        return 2
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    db = SessionLocal()
    try:
        counts = seed_payload(db, payload)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("Seed applied:")
    for k, v in counts.items():
        print(f"  {k:>13s}: {v}")

    if args.skip_validate:
        print("Skipped validate_seed (--skip-validate set).")
        return 0

    db = SessionLocal()
    try:
        errors = validate_seed(db)
    finally:
        db.close()

    if errors:
        print("\nINTEGRITY CHECK FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("Integrity check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
