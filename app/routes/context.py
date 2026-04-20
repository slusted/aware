"""Routes for the two 'context' scopes — company and customer.
  - GET  /api/context/{scope}          — current brief + metadata
  - GET  /api/context/{scope}/history  — prior briefs
  - POST /api/context/{scope}/briefs   — regenerate (background)
  - GET  /api/context/{scope}/docs     — list uploaded docs
  - POST /api/context/{scope}/docs     — upload a doc (multipart)
  - DELETE /api/context/{scope}/docs/{id} — remove a doc

Docs are stored under data/context/<scope>/ on disk; a Document row records
metadata. doc_processor runs in the background to produce a summary.
"""
import os
import shutil
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..deps import get_db, get_current_user, require_role
from ..models import ContextBrief, Document, Run

router = APIRouter(prefix="/api/context", tags=["context"])

SCOPES = {"company", "customer"}
DATA_ROOT = Path(os.environ.get("DATA_DIR", "data")) / "context"


def _require_scope(scope: str):
    if scope not in SCOPES:
        raise HTTPException(404, f"unknown scope '{scope}'")


def _scope_dir(scope: str) -> Path:
    d = DATA_ROOT / scope
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("/{scope}")
def current_brief(scope: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _require_scope(scope)
    latest = (
        db.query(ContextBrief)
        .filter(ContextBrief.scope == scope)
        .order_by(ContextBrief.created_at.desc())
        .first()
    )
    return {
        "scope": scope,
        "brief": {
            "id": latest.id,
            "body_md": latest.body_md,
            "source_summary": latest.source_summary,
            "model": latest.model,
            "created_at": latest.created_at,
        } if latest else None,
    }


@router.get("/{scope}/history")
def history(scope: str, limit: int = 20, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _require_scope(scope)
    rows = (
        db.query(ContextBrief)
        .filter(ContextBrief.scope == scope)
        .order_by(ContextBrief.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {"id": r.id, "created_at": r.created_at, "model": r.model,
         "source_summary": r.source_summary, "body_md": r.body_md}
        for r in rows
    ]


def _regen_scope(scope: str):
    from ..db import SessionLocal
    from ..context_briefs import synthesize
    import json
    db = SessionLocal()
    try:
        cfg_path = os.environ.get("CONFIG_PATH", "config.json")
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        synthesize(db, scope,
                   company=cfg.get("company", "Seek"),
                   industry=cfg.get("industry", "job search and recruitment platforms"))
    finally:
        db.close()


@router.post("/{scope}/briefs", status_code=202)
def regenerate(scope: str, bg: BackgroundTasks, _=Depends(require_role("admin", "analyst"))):
    _require_scope(scope)
    bg.add_task(_regen_scope, scope)
    return {"queued": True, "scope": scope}


@router.get("/{scope}/docs")
def list_docs(scope: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _require_scope(scope)
    rows = (
        db.query(Document)
        .filter(Document.bucket == scope)
        .order_by(Document.created_at.desc())
        .all()
    )
    return [
        {
            "id": d.id, "filename": d.filename, "content_type": d.content_type,
            "size_bytes": d.size_bytes, "summary": (d.summary or "")[:400],
            "has_summary": bool(d.summary), "created_at": d.created_at,
        }
        for d in rows
    ]


def _process_doc(doc_id: int):
    """Background task: extract text from uploaded file and cache summary on the row."""
    from ..db import SessionLocal
    db = SessionLocal()
    try:
        d = db.get(Document, doc_id)
        if not d:
            return
        path = DATA_ROOT / d.bucket / d.filename
        if not path.exists():
            return
        text = ""
        try:
            from doc_processor import extract_text  # engine helper
            text = extract_text(str(path)) or ""
        except Exception:
            try:
                if path.suffix.lower() in (".md", ".txt"):
                    text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass
        d.summary = text[:8000] if text else None
        db.commit()
    finally:
        db.close()


@router.post("/{scope}/docs", status_code=201)
async def upload_doc(
    scope: str,
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    user=Depends(require_role("admin", "analyst")),
    db: Session = Depends(get_db),
):
    _require_scope(scope)
    safe_name = Path(file.filename).name  # strip any path components
    if not safe_name:
        raise HTTPException(400, "empty filename")

    target_dir = _scope_dir(scope)
    dest = target_dir / safe_name
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    d = Document(
        bucket=scope,
        filename=safe_name,
        content_type=file.content_type,
        size_bytes=dest.stat().st_size,
        uploaded_by=user.id,
        created_at=datetime.utcnow(),
    )
    db.add(d)
    db.commit()
    db.refresh(d)

    bg.add_task(_process_doc, d.id)
    return {
        "id": d.id, "filename": d.filename, "size_bytes": d.size_bytes,
        "processing": True,
    }


@router.get("/customer/watch")
def get_customer_watch(_=Depends(get_current_user)):
    """Read the customer_watch block from config.json."""
    import json
    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("customer_watch", {
        "enabled": True, "subreddits": [],
        "max_results_per_source": 6,
    })


class CustomerWatchIn(__import__("pydantic").BaseModel):
    enabled: bool = True
    subreddits: list[str] = []
    max_results_per_source: int = 6


@router.put("/customer/watch")
def put_customer_watch(payload: CustomerWatchIn, _=Depends(require_role("admin", "analyst"))):
    """Update the customer_watch block in config.json. Takes effect on next scan."""
    import json
    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    # Reddit-only going forward; drop any legacy twitter_queries field so the
    # config stays minimal and doesn't confuse future readers.
    new_block = payload.model_dump()
    existing = cfg.get("customer_watch") or {}
    if "twitter_queries" in existing:
        print("[customer_watch] dropping legacy twitter_queries from config")
    cfg["customer_watch"] = new_block
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return {"ok": True}


@router.post("/customer/scan", status_code=202)
def trigger_customer_scan(
    bg: BackgroundTasks,
    days: int | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin", "analyst")),
):
    """End-to-end customer-side scan: sweep configured subreddits + twitter
    queries → save findings → refresh the customer brief. Shows up as a Run
    (kind='customer_scan') with live event streaming, same shape as the
    per-competitor scan. Rejected if any other run is in flight."""
    existing = db.query(Run).filter(Run.status.in_(["running", "cancelling"])).first()
    if existing:
        raise HTTPException(
            409,
            detail=f"run #{existing.id} ({existing.kind}) is already in flight — wait for it to finish",
        )
    from ..jobs import run_customer_scan_job
    bg.add_task(run_customer_scan_job, "manual", days)
    return {"queued": True, "kind": "customer_scan", "days": days}


@router.delete("/{scope}/docs/{doc_id}", status_code=204)
def delete_doc(
    scope: str, doc_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_role("admin")),
):
    _require_scope(scope)
    d = db.get(Document, doc_id)
    if not d or d.bucket != scope:
        raise HTTPException(404)
    path = DATA_ROOT / scope / d.filename
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass
    db.delete(d)
    db.commit()
