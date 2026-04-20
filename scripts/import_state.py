"""One-shot migration from the file-based state into SQLite.

Reads:
  - config.json         → competitors, users (team)
  - data/seen_items.json → recent findings (best-effort, by competitor)
  - data/reports/*.md   → reports archive
  - skill/SKILL.md      → initial skill row

Idempotent: re-running is safe (skips rows that already exist).

Usage:
  python scripts/import_state.py
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Base, SessionLocal, engine
from app.models import User, Competitor, Report, Skill, Finding


def _config() -> dict:
    with open(ROOT / "config.json") as f:
        return json.load(f)


def import_users(db, cfg):
    for m in cfg.get("team", []):
        email = m["email"]
        if db.query(User).filter(User.email == email).first():
            continue
        db.add(User(email=email, name=m.get("name", email), role="viewer"))
    # Ensure stub admin exists for the deps.get_current_user fallback
    if not db.query(User).filter(User.email == "admin@local").first():
        db.add(User(email="admin@local", name="Admin", role="admin"))


def import_competitors(db, cfg):
    for c in cfg.get("competitors", []):
        name = c["name"]
        existing = db.query(Competitor).filter(Competitor.name == name).first()
        if existing:
            continue
        db.add(Competitor(
            name=name,
            category=c.get("category"),
            source=c.get("_source", "manual"),
            discovered_date=c.get("_discovered_date"),
            threat_angle=c.get("_threat_angle"),
            keywords=c.get("keywords", []),
            subreddits=c.get("subreddits", []),
            careers_domains=c.get("careers_domains", []),
            newsroom_domains=c.get("newsroom_domains", []),
            active=True,
        ))


def import_reports(db):
    reports_dir = ROOT / "data" / "reports"
    if not reports_dir.exists():
        return
    for md in sorted(reports_dir.glob("report_*.md")):
        if db.query(Report).filter(Report.file_path == str(md)).first():
            continue
        stamp = md.stem.removeprefix("report_")
        try:
            created = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
        except ValueError:
            created = datetime.utcfromtimestamp(md.stat().st_mtime)
        db.add(Report(
            title=f"Scan {created:%Y-%m-%d %H:%M}",
            body_md=md.read_text(encoding="utf-8"),
            file_path=str(md),
            created_at=created,
        ))


def import_skill(db):
    skill_path = ROOT / "skill" / "SKILL.md"
    if not skill_path.exists():
        return
    if db.query(Skill).filter(Skill.name == "competitor-watch").first():
        return
    db.add(Skill(
        name="competitor-watch",
        version=1,
        body_md=skill_path.read_text(encoding="utf-8"),
        active=True,
    ))


def import_recent_findings(db):
    """Seed the findings table with the last-run items so the dashboard
    isn't empty before the first fresh scan runs."""
    last = ROOT / "data" / "last_findings.json"
    if not last.exists():
        return
    try:
        items = json.loads(last.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(items, list):
        return
    from app.signals.extract import classify as _classify
    for f in items:
        h = f.get("hash")
        if not h or db.query(Finding).filter(Finding.hash == h).first():
            continue
        st, mat, payload = _classify(f)
        db.add(Finding(
            competitor=f.get("competitor", "unknown"),
            source=f.get("source", ""),
            topic=f.get("topic"),
            title=f.get("title"),
            url=f.get("url"),
            content=f.get("content"),
            hash=h,
            signal_type=st,
            materiality=mat,
            payload=payload,
        ))


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    cfg = _config()
    try:
        import_users(db, cfg)
        import_competitors(db, cfg)
        import_reports(db)
        import_skill(db)
        import_recent_findings(db)
        db.commit()
        print(f"users:       {db.query(User).count()}")
        print(f"competitors: {db.query(Competitor).count()}")
        print(f"reports:     {db.query(Report).count()}")
        print(f"findings:    {db.query(Finding).count()}")
        print(f"skills:      {db.query(Skill).count()}")
        print("done.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
