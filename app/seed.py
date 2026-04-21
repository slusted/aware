"""Idempotent competitor seeding from the repo's committed config.json.

Runs on every app boot (after migrations) so a failed release phase can't
leave the web view empty. The release phase in Procfile still calls
import_state.py for reports/skills/users; competitors are handled here too
to give the daemon a self-healing path.

Rows are keyed by name. For each competitor in the committed config.json:
  - missing → insert active
  - exists inactive → reactivate (the committed list is the dev's source of
    truth; UI-side soft deletes rewrite CONFIG_PATH on the volume, not this
    bundled copy, so reactivation here only revives what git still lists)
  - exists active → no-op
"""
import json
from pathlib import Path
from sqlalchemy.orm import Session

from .models import Competitor


REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.json"


def seed_competitors(db: Session) -> tuple[int, int]:
    if not REPO_CONFIG.exists():
        return 0, 0
    try:
        cfg = json.loads(REPO_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return 0, 0
    added = reactivated = 0
    for c in cfg.get("competitors", []):
        name = c.get("name")
        if not name:
            continue
        existing = db.query(Competitor).filter(Competitor.name == name).first()
        if existing:
            if not existing.active:
                existing.active = True
                reactivated += 1
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
            homepage_domain=c.get("homepage_domain"),
            active=True,
        ))
        added += 1
    db.commit()
    return added, reactivated
