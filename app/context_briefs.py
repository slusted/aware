"""Synthesizer for Company and Customer briefs.

Each brief is a "living view" of one side of the strategy triangle:
  - company:  Seek's (the reader's) own posture, priorities, constraints
  - customer: what the market's candidates + employers actually want

Inputs combined:
  - Uploaded Documents (PDFs, DOCX, MD) scoped to this bucket
  - Their processed summaries (strategy_profiles.json, via doc_processor)
  - Recent findings relevant to the scope (for customer: VoC findings)
  - The previous brief as delta baseline
  - The scope's skill (editable in /settings/skills)

Each brief is appended; "current" = latest. Regenerate on scan completion
or on demand via the scope's page.
"""
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy.orm import Session

from .models import ContextBrief, Document, Finding
from .skills import load_active

MODEL = "claude-sonnet-4-6"

_DEFAULTS = {
    "recency_days_brief": 120,
    "max_findings_brief": 40,
}


def _limits() -> dict:
    """Read brief limits from config.json on demand. Edits take effect without
    restart; failures fall through to defaults so a missing/broken config.json
    never wedges brief generation."""
    try:
        from service import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    block = cfg.get("limits") or {}
    return {k: block.get(k, v) for k, v in _DEFAULTS.items()}


SCOPE_CONFIG = {
    "company": {
        "skill":       "company_brief",
        "doc_bucket":  "company",
        "finding_filter": None,
        "preface_noun": "the company's own",
    },
    "customer": {
        "skill":       "customer_brief",
        "doc_bucket":  "customer",
        # Pull both per-competitor VoC findings AND aggregated category
        # discussion from the dedicated customer-watch sweep.
        "finding_filter": ["voice of customer", "customer_discussion"],
        "preface_noun": "the customer",
    },
}


def _load_analyzer_client():
    import analyzer
    return analyzer.client


def _gather_docs(db: Session, bucket: str) -> list[Document]:
    return (
        db.query(Document)
        .filter(Document.bucket == bucket)
        .order_by(Document.created_at.desc())
        .limit(20)
        .all()
    )


def _gather_findings(db: Session, topic_filter) -> list[Finding]:
    lim = _limits()
    q = db.query(Finding).filter(
        Finding.created_at >= datetime.utcnow() - timedelta(days=int(lim["recency_days_brief"]))
    )
    if isinstance(topic_filter, (list, tuple, set)):
        q = q.filter(Finding.topic.in_(list(topic_filter)))
    elif topic_filter:
        q = q.filter(Finding.topic == topic_filter)
    return q.order_by(Finding.created_at.desc()).limit(int(lim["max_findings_brief"])).all()


def _gather_prior(db: Session, scope: str) -> ContextBrief | None:
    return (
        db.query(ContextBrief)
        .filter(ContextBrief.scope == scope)
        .order_by(ContextBrief.created_at.desc())
        .first()
    )


def _build_prompt(scope: str, company: str, industry: str,
                  docs: list[Document], findings: list[Finding],
                  prior: ContextBrief | None) -> tuple[str, str]:
    cfg = SCOPE_CONFIG[scope]
    skill_body = load_active(cfg["skill"]).replace("{company}", company)

    system = (
        f"You are a strategy analyst. Your reader is the {company} leadership "
        f"team ({industry}).\n\n{skill_body}"
    )

    doc_text = "\n\n".join(
        f"### {d.filename}\n{(d.summary or '(no extracted summary yet)')[:2000]}"
        for d in docs
    ) or "(no documents uploaded yet for this scope)"

    # Per-finding chars for brief inputs. Env tunable.
    import os as _os
    _brief_chars = int(_os.environ.get("FINDING_CHARS_BRIEF", "2500"))
    findings_text = "\n".join(
        f"- [{f.created_at:%Y-%m-%d} · {f.source}] {f.title or ''} — {(f.content or '')[:_brief_chars]}"
        for f in findings
    ) or "(no relevant findings in window)"

    prior_text = prior.body_md if prior else "(no prior brief)"

    user = f"""# Scope: {scope}

---
# Uploaded source documents
{doc_text}

---
# Recent signals (last {_limits()["recency_days_brief"]} days)
{findings_text}

---
# Prior brief
{prior_text}

---
Produce a fresh {cfg['preface_noun']} brief. Follow the structure in the system
prompt exactly. If inputs are thin, say so in each relevant section — don't
fabricate. Emphasize what's changed since the prior brief.
"""
    return system, user


def synthesize(db: Session, scope: str, *,
               run_id: int | None = None,
               company: str = "Seek",
               industry: str = "job search and recruitment platforms") -> ContextBrief | None:
    """Generate one brief for a scope. Returns None if there's nothing to work
    with (no docs, no findings, no prior) — saves tokens."""
    if scope not in SCOPE_CONFIG:
        raise ValueError(f"unknown scope: {scope}")
    cfg = SCOPE_CONFIG[scope]

    docs = _gather_docs(db, cfg["doc_bucket"])
    findings = _gather_findings(db, cfg["finding_filter"])
    prior = _gather_prior(db, scope)

    if not docs and not findings and not prior:
        return None

    client = _load_analyzer_client()
    system, user = _build_prompt(scope, company, industry, docs, findings, prior)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    body = resp.content[0].text

    source_summary = (
        f"docs={len(docs)} · findings={len(findings)} · "
        f"prior={'yes' if prior else 'no'}"
    )

    cb = ContextBrief(
        scope=scope,
        run_id=run_id,
        body_md=body,
        source_summary=source_summary,
        model=MODEL,
    )
    db.add(cb)
    db.commit()
    db.refresh(cb)
    return cb


def current(db: Session, scope: str) -> ContextBrief | None:
    return (
        db.query(ContextBrief)
        .filter(ContextBrief.scope == scope)
        .order_by(ContextBrief.created_at.desc())
        .first()
    )
