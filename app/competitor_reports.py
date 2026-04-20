"""Per-competitor 'overall strategy review' synthesizer.

Gathers:
  - last ~90 days of findings for the competitor
  - recent market digests (the Report table) for context
  - the previous CompetitorReport (if any) as 'prior view'
  - the competitor's own profile metadata

Produces a fresh markdown strategy review via Claude Sonnet 4.6.
"""
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from .models import Competitor, CompetitorReport, Finding, Report

MODEL = "claude-sonnet-4-6"

# Defaults used when config.json.limits is missing a value. Live tuning
# happens in config.json — see _limits() below.
_DEFAULTS = {
    "recency_days_report": 90,
    "max_findings_report": 60,
    "max_market_digests_report": 6,
}


def _limits() -> dict:
    """Read limits from config.json on demand. Resolved per call (cheap: tens
    of kB JSON) so edits to config.json take effect without process restart."""
    try:
        from service import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    block = cfg.get("limits") or {}
    return {k: block.get(k, v) for k, v in _DEFAULTS.items()}


def _load_analyzer_client():
    """Use the already-instrumented client from analyzer so Claude calls
    are auto-logged to usage_events via the startup monkey-patch."""
    import analyzer
    return analyzer.client


def _gather(db: Session, competitor: Competitor) -> dict:
    lim = _limits()
    since = datetime.utcnow() - timedelta(days=int(lim["recency_days_report"]))
    findings = (
        db.query(Finding)
        .filter(Finding.competitor == competitor.name)
        .filter(Finding.created_at >= since)
        .order_by(Finding.created_at.desc())
        .limit(int(lim["max_findings_report"]))
        .all()
    )
    digests = (
        db.query(Report)
        .order_by(Report.created_at.desc())
        .limit(int(lim["max_market_digests_report"]))
        .all()
    )
    prior = (
        db.query(CompetitorReport)
        .filter(CompetitorReport.competitor_id == competitor.id)
        .order_by(CompetitorReport.created_at.desc())
        .first()
    )
    return {"findings": findings, "digests": digests, "prior": prior}


def _extract_competitor_mentions(digest_md: str, competitor_name: str) -> str:
    """Pull only the paragraphs / sections of a market digest that mention the
    competitor. Keeps token count reasonable when feeding several digests."""
    if not digest_md:
        return ""
    needle = competitor_name.lower()
    blocks = digest_md.split("\n\n")
    hits = [b for b in blocks if needle in b.lower()]
    # Hard cap to avoid runaway context
    return "\n\n".join(hits)[:4000]


def _load_system_prompt(company: str, industry: str) -> str:
    """System prompt body comes from the `competitor_review` skill, which is
    editable in /settings/skills. We prepend the reader-identity line so the
    skill body doesn't need to know who the company is."""
    from .skills import load_active
    skill_body = load_active("competitor_review") or ""
    preface = (
        f"You are a competitive intelligence analyst for {company} "
        f"({industry}). Your reader is the {company} leadership team.\n\n"
    )
    # Let the skill substitute {company} as a soft placeholder if it wants.
    skill_body = skill_body.replace("{company}", company)
    return preface + skill_body


def _build_prompt(competitor: Competitor, inputs: dict, company: str, industry: str) -> tuple[str, str]:
    findings = inputs["findings"]
    digests = inputs["digests"]
    prior = inputs["prior"]

    # Per-finding chars for the review prompt. Env tunable: raise for richer
    # analysis, lower to control per-competitor token cost.
    import os as _os
    _review_chars = int(_os.environ.get("FINDING_CHARS_REVIEW", "2500"))
    findings_text = "\n".join(
        f"- [{f.created_at:%Y-%m-%d} · {f.source}] {f.title or ''} — {(f.content or '')[:_review_chars]}"
        for f in findings
    ) or "(no findings in window)"

    digest_snippets = []
    for d in digests:
        excerpt = _extract_competitor_mentions(d.body_md, competitor.name)
        if excerpt:
            digest_snippets.append(f"## Market digest {d.created_at:%Y-%m-%d}\n{excerpt}")
    digest_text = "\n\n".join(digest_snippets) or "(no recent mentions in market digests)"

    prior_text = prior.body_md if prior else "(no prior review)"

    system = _load_system_prompt(company, industry)

    user = f"""# Competitor: {competitor.name}
**Category:** {competitor.category or 'unspecified'}
**Threat angle:** {competitor.threat_angle or '(none recorded)'}

---
# Prior strategy review
{prior_text}

---
# Findings — last {_limits()["recency_days_report"]} days ({len(findings)} items, most recent first)
{findings_text}

---
# Mentions in recent market digests
{digest_text}

---
Produce a fresh overall strategy review of {competitor.name}. Emphasize the
last 4–6 weeks but contextualize with the full window. If there's no new
signal vs. the prior review, say so plainly — don't invent movement.
"""
    return system, user


def synthesize(db: Session, competitor: Competitor, *,
               run_id: int | None = None,
               company: str = "Seek",
               industry: str = "job search and recruitment platforms") -> CompetitorReport | None:
    """Generate one strategy review. Returns None if there's no material at all
    (no findings in window AND no prior review) — nothing worth spending tokens on."""
    inputs = _gather(db, competitor)
    if not inputs["findings"] and not inputs["prior"]:
        return None

    client = _load_analyzer_client()
    system, user = _build_prompt(competitor, inputs, company, industry)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    body = resp.content[0].text

    source_summary = (
        f"findings={len(inputs['findings'])} · "
        f"market_digests_with_mentions={sum(1 for d in inputs['digests'] if _extract_competitor_mentions(d.body_md, competitor.name))} · "
        f"prior={'yes' if inputs['prior'] else 'no'}"
    )

    cr = CompetitorReport(
        competitor_id=competitor.id,
        run_id=run_id,
        body_md=body,
        source_summary=source_summary,
        model=MODEL,
    )
    db.add(cr)
    db.commit()
    db.refresh(cr)

    # Distill a 2-sentence threat angle from the fresh review. Cheap haiku
    # call; used as the salient overall view on competitor cards + passed to
    # scanner via config.json._threat_angle.
    try:
        threat = _distill_threat_angle(client, competitor, body, company)
        if threat:
            competitor.threat_angle = threat
            db.commit()
    except Exception as e:
        # Non-fatal — review is the primary output. Log but continue.
        print(f"[review] threat-angle distill failed for {competitor.name}: {e}")

    return cr


def _distill_threat_angle(client, competitor: Competitor, review_body: str, company: str) -> str | None:
    """Ask a cheaper model to produce a 2-sentence 'who they are + why they
    matter to us' summary. Runs once per competitor per scan — ~200 output
    tokens at haiku pricing, so negligible cost."""
    system = (
        f"You write 2-sentence threat-angle summaries for {company}'s "
        f"competitive intelligence dashboard. Sentence 1: who the competitor "
        f"is (positioning + scale). Sentence 2: the specific reason they "
        f"matter to {company} — the strategic implication, not a feature "
        f"list. Plain nouns, analyst tone. No hedging. No 'could potentially'. "
        f"Output ONLY the 2 sentences — no preface, no header, no quotes."
    )
    user = f"Competitor: {competitor.name}\nCategory: {competitor.category or 'unspecified'}\n\n# Current strategy review\n{review_body}\n\n---\nProduce the 2-sentence threat angle."
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = (resp.content[0].text or "").strip()
    # Strip any stray quoting or leading/trailing whitespace artifacts.
    text = text.strip('"').strip("'").strip()
    if not text or len(text) > 600:
        return None
    return text


def synthesize_all_active(db: Session, *, run_id: int | None = None,
                          company: str = "Seek",
                          industry: str = "job search and recruitment platforms") -> dict:
    """Regenerate reports for every active competitor with *some* recent
    activity. Failures per competitor are isolated — one blow-up doesn't kill
    the whole batch."""
    since = datetime.utcnow() - timedelta(days=int(_limits()["recency_days_report"]))
    active = (
        db.query(Competitor)
        .filter(Competitor.active == True)
        .order_by(Competitor.name)
        .all()
    )
    summary = {"generated": [], "skipped": [], "errors": []}
    for c in active:
        has_recent = (
            db.query(Finding)
            .filter(Finding.competitor == c.name)
            .filter(Finding.created_at >= since)
            .first()
            is not None
        )
        if not has_recent:
            summary["skipped"].append(c.name)
            continue
        try:
            cr = synthesize(db, c, run_id=run_id, company=company, industry=industry)
            if cr:
                summary["generated"].append(c.name)
            else:
                summary["skipped"].append(c.name)
        except Exception as e:
            summary["errors"].append({"competitor": c.name, "error": str(e)})
    return summary
