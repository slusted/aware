"""Keep config.json in lockstep with the competitors table.
The existing engine (scanner/service) still reads config.json at scan time —
this module is the bridge so UI edits land in the place the engine looks."""
import json
import os
from pathlib import Path
from sqlalchemy.orm import Session

from .models import Competitor

CONFIG_PATH = Path(os.environ.get(
    "CONFIG_PATH",
    Path(__file__).resolve().parent.parent / "config.json",
))


def _read() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _write(cfg: dict):
    # Preserve formatting the engine code already works with.
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def sync_db_to_config(db: Session):
    """Rewrite config.json competitors[] from active DB rows. Other top-level
    keys (company, industry, scan_hour, team, watch_topics) are preserved."""
    cfg = _read()
    rows = db.query(Competitor).filter(Competitor.active == True).order_by(Competitor.name).all()
    cfg["competitors"] = [_to_json(c) for c in rows]
    _write(cfg)


def _to_json(c: Competitor) -> dict:
    """Shape mirrors what config.json has today (see existing entries)."""
    entry = {
        "name": c.name,
        "keywords": list(c.keywords or []),
        "subreddits": list(c.subreddits or []),
        "careers_domains": list(c.careers_domains or []),
        "newsroom_domains": list(c.newsroom_domains or []),
    }
    if c.homepage_domain:
        entry["homepage_domain"] = c.homepage_domain
    if c.category:
        entry["category"] = c.category
    if c.source and c.source != "manual":
        entry["_source"] = c.source
    if c.discovered_date:
        entry["_discovered_date"] = c.discovered_date
    if c.threat_angle:
        entry["_threat_angle"] = c.threat_angle
    # Per-competitor score thresholds. Only written when set (so the global
    # env defaults remain authoritative for untuned competitors).
    if c.min_relevance_score is not None:
        entry["min_relevance_score"] = float(c.min_relevance_score)
    if c.social_score_multiplier is not None:
        entry["social_score_multiplier"] = float(c.social_score_multiplier)
    return entry
