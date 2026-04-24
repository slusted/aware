"""Skill loader: DB-first, file-fallback, built-in-fallback.

Skills are system prompts that drive LLM calls. Two are used today:
  - 'market_digest'     → analyzer.analyze_findings (cross-competitor digest)
  - 'competitor_review' → competitor_reports.synthesize (per-competitor review)

Editing happens through /settings/skills (writes a new Skill row, bumps
version, marks it active). The engine reads the active row at call time.

Seeding on startup: if the DB has no active row for a skill name, we read
the file in skill/<name>.md and insert it as version 1. After that, the DB
is authoritative — file edits are ignored unless the row is deleted.
"""
import os
from pathlib import Path
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Skill

SKILL_DIR = Path(os.environ.get(
    "SKILL_DIR",
    Path(__file__).resolve().parent.parent / "skill",
))

# Map: skill name → (file name, short description for the UI)
KNOWN_SKILLS: dict[str, tuple[str, str]] = {
    "market_digest": (
        "market_digest.md",
        "Drives the cross-competitor daily digest emailed to the team.",
    ),
    "competitor_review": (
        "competitor_review.md",
        "Drives the per-competitor 'overall strategy review' on each profile page.",
    ),
    "company_brief": (
        "company_brief.md",
        "Drives the company's own strategic brief, synthesized from uploaded public docs + recent signals.",
    ),
    "customer_brief": (
        "customer_brief.md",
        "Drives the customer brief — synthesized view of what customers want and how they're shifting.",
    ),
    "positioning_extract": (
        "positioning_extract.md",
        "Structured extraction of positioning pillars from a competitor's own marketing pages. Call 1 of the positioning pipeline.",
    ),
    "positioning_narrative": (
        "positioning_narrative.md",
        "Narrative synthesis of current positioning and diff vs. the prior snapshot. Call 2 of the positioning pipeline.",
    ),
    "deep_research_brief": (
        "deep_research_brief.md",
        "Brief sent to Gemini Deep Research for an investor-grade per-competitor dossier. Placeholders: {{competitor_name}}, {{category}}, {{our_company}}, {{our_industry}}, {{threat_angle}}, {{watch_topics}}.",
    ),
    "discover_competitors": (
        "discover_competitors.md",
        "Drives the 'Discover new competitors' tool-use loop on Manage Watchlist — surfaces up to 8 candidate competitors with verified homepages and cited evidence. Placeholders: {{our_company}}, {{our_industry}}, {{existing_list}}, {{dismissed_list}}, {{hint}}.",
    ),
}


def _strip_frontmatter(text: str) -> str:
    """Remove `---` YAML frontmatter if present."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].lstrip()
    return text


def _read_file(name: str) -> str:
    fname, _ = KNOWN_SKILLS.get(name, (f"{name}.md", ""))
    path = SKILL_DIR / fname
    if not path.exists():
        return ""
    return _strip_frontmatter(path.read_text(encoding="utf-8"))


def load_active(name: str) -> str:
    """Return the active skill body for `name`. Tries DB → file → empty string.
    Callers should treat an empty string as 'no skill' and behave reasonably."""
    db = SessionLocal()
    try:
        row = (
            db.query(Skill)
            .filter(Skill.name == name, Skill.active == True)
            .order_by(Skill.version.desc())
            .first()
        )
        if row and row.body_md:
            return row.body_md
    finally:
        db.close()
    return _read_file(name)


def save_new_version(name: str, body_md: str) -> Skill:
    """Append a new version for `name`, make it active, deactivate previous.
    Returns the new Skill row."""
    db = SessionLocal()
    try:
        max_ver = (
            db.query(Skill)
            .filter(Skill.name == name)
            .order_by(Skill.version.desc())
            .first()
        )
        next_ver = (max_ver.version + 1) if max_ver else 1
        db.query(Skill).filter(Skill.name == name, Skill.active == True).update(
            {Skill.active: False}
        )
        row = Skill(name=name, version=next_ver, body_md=body_md, active=True)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def activate_version(name: str, version: int) -> Skill | None:
    """Flip an older version back to active (for restore). Returns the row."""
    db = SessionLocal()
    try:
        target = db.query(Skill).filter(
            Skill.name == name, Skill.version == version
        ).first()
        if not target:
            return None
        db.query(Skill).filter(Skill.name == name, Skill.active == True).update(
            {Skill.active: False}
        )
        target.active = True
        db.commit()
        db.refresh(target)
        return target
    finally:
        db.close()


def sync_files_to_db():
    """Called once at app startup. For each known skill, if no active DB row
    exists, seed version 1 from the file on disk. Idempotent."""
    db = SessionLocal()
    try:
        for name in KNOWN_SKILLS:
            existing = (
                db.query(Skill)
                .filter(Skill.name == name, Skill.active == True)
                .first()
            )
            if existing:
                continue
            body = _read_file(name)
            if not body:
                print(f"  [skills] no file for {name}, skipping seed")
                continue
            db.add(Skill(name=name, version=1, body_md=body, active=True))
            print(f"  [skills] seeded {name} from file (v1)")
        db.commit()
    finally:
        db.close()
