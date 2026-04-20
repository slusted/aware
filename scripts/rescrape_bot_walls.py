"""One-shot cleanup: re-fetch Findings whose stored content is a bot-wall /
challenge page.

Scans the findings table, flags rows whose content trips the sanitize
is_bot_wall or is_chrome filters, and re-runs them through app.fetcher. On success, the
row's content + hash are rewritten in place. If the new hash collides with a
different finding's hash (same content arrived cleanly elsewhere), the
bot-wall row is deleted instead.

Usage:
  python scripts/rescrape_bot_walls.py           # dry-run, report only
  python scripts/rescrape_bot_walls.py --apply   # actually rewrite
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Windows console defaults to cp1252; reconfigure so the fetcher's unicode
# summary line (→, ·) doesn't crash the run.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import Finding
from app import fetcher
from scanner import content_hash
from app.adapters.fetch.sanitize import is_bot_wall, is_chrome


def _looks_broken(text: str | None) -> bool:
    if not text:
        return False
    return is_bot_wall(text) or is_chrome(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually rewrite rows; without this flag, only reports")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap number of rows processed (0 = no cap)")
    args = ap.parse_args()

    # Load fetcher config so the ZenRows/ScrapingBee toggles match the live app.
    try:
        with open(ROOT / "config.json", encoding="utf-8") as f:
            fetcher.configure(json.load(f))
    except FileNotFoundError:
        fetcher.configure({})

    db = SessionLocal()
    try:
        rows = (db.query(Finding)
                  .filter(Finding.content.isnot(None))
                  .filter(Finding.url.isnot(None))
                  .order_by(Finding.id.desc())
                  .all())
    finally:
        db.close()

    broken = [r for r in rows if _looks_broken(r.content)]
    print(f"[scan] {len(rows)} findings with content · {len(broken)} look broken")
    if args.limit:
        broken = broken[:args.limit]
        print(f"[scan] limiting to {len(broken)} rows")

    if not broken:
        return 0

    urls = list({r.url for r in broken if r.url})
    print(f"[fetch] re-fetching {len(urls)} unique URLs")
    fetched = fetcher.bulk_fetch(urls, concurrency=4)

    if not args.apply:
        good = sum(1 for u in urls if fetched.get(u, (None, ""))[0])
        print(f"[dry-run] would rewrite {good}/{len(urls)} URLs — pass --apply to commit")
        # Show a sample
        for r in broken[:5]:
            res = fetched.get(r.url) or (None, "failed")
            new_len = len(res[0]) if res[0] else 0
            print(f"  id={r.id} old={len(r.content)}ch new={new_len}ch src={res[1]} {r.url[:80]}")
        return 0

    rewrote = deleted = fetch_failed = 0
    db = SessionLocal()
    try:
        for r in broken:
            res = fetched.get(r.url)
            if not res or not res[0]:
                fetch_failed += 1
                continue
            new_content, src = res
            new_hash = content_hash(new_content)

            if new_hash == r.hash:
                # Extracted content happens to lowercase-strip to the same hash —
                # skip, nothing to rewrite.
                continue

            row = db.get(Finding, r.id)
            if row is None:
                continue
            row.content = new_content
            row.hash = new_hash
            try:
                db.commit()
                rewrote += 1
                print(f"  ok id={r.id} {len(r.content)}→{len(new_content)}ch via {src}")
            except IntegrityError:
                db.rollback()
                # Hash collides with a finding that already has the clean
                # content — delete the bot-wall row, keep the good one.
                row = db.get(Finding, r.id)
                if row is not None:
                    db.delete(row)
                    db.commit()
                    deleted += 1
                    print(f"  dup id={r.id} — deleted (clean copy already exists)")
    finally:
        db.close()

    print(f"[done] rewrote={rewrote} deleted={deleted} fetch_failed={fetch_failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
