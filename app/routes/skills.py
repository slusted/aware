from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..deps import get_db, get_current_user, require_role
from ..models import Skill as SkillModel
from .. import skills as skills_mod

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillIn(BaseModel):
    body_md: str


@router.get("")
def list_skills(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Return a row per known skill with its active version (if any) + count."""
    out = []
    for name, (_fname, desc) in skills_mod.KNOWN_SKILLS.items():
        active = (
            db.query(SkillModel)
            .filter(SkillModel.name == name, SkillModel.active == True)
            .order_by(SkillModel.version.desc())
            .first()
        )
        total = db.query(SkillModel).filter(SkillModel.name == name).count()
        out.append({
            "name": name,
            "description": desc,
            "active_version": active.version if active else None,
            "active_body": active.body_md if active else "",
            "updated_at": active.created_at if active else None,
            "total_versions": total,
        })
    return out


@router.get("/{name}")
def get_skill(name: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    if name not in skills_mod.KNOWN_SKILLS:
        raise HTTPException(404, f"unknown skill '{name}'")
    active = (
        db.query(SkillModel)
        .filter(SkillModel.name == name, SkillModel.active == True)
        .order_by(SkillModel.version.desc())
        .first()
    )
    return {
        "name": name,
        "description": skills_mod.KNOWN_SKILLS[name][1],
        "active_version": active.version if active else None,
        "body_md": active.body_md if active else skills_mod.load_active(name),
        "updated_at": active.created_at if active else None,
    }


@router.get("/{name}/versions")
def list_versions(name: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    if name not in skills_mod.KNOWN_SKILLS:
        raise HTTPException(404, f"unknown skill '{name}'")
    rows = (
        db.query(SkillModel)
        .filter(SkillModel.name == name)
        .order_by(SkillModel.version.desc())
        .all()
    )
    return [
        {
            "id": r.id, "version": r.version, "active": r.active,
            "created_at": r.created_at, "preview": (r.body_md or "")[:200],
        }
        for r in rows
    ]


@router.post("/{name}")
def save_skill(
    name: str, payload: SkillIn,
    _=Depends(require_role("admin", "analyst")),
):
    if name not in skills_mod.KNOWN_SKILLS:
        raise HTTPException(404, f"unknown skill '{name}'")
    if not payload.body_md.strip():
        raise HTTPException(400, "body_md cannot be empty")
    row = skills_mod.save_new_version(name, payload.body_md)
    return {"name": name, "version": row.version, "active": True}


@router.post("/{name}/activate/{version}")
def restore_version(
    name: str, version: int,
    _=Depends(require_role("admin", "analyst")),
):
    if name not in skills_mod.KNOWN_SKILLS:
        raise HTTPException(404, f"unknown skill '{name}'")
    row = skills_mod.activate_version(name, version)
    if not row:
        raise HTTPException(404, f"version {version} not found")
    return {"name": name, "version": row.version, "active": True}
