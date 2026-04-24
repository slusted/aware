"""Brief composer for the cross-competitor market synthesis (Spec 05).

Builds the payload sent to Gemini Deep Research for `/market` synthesis runs
by stitching together four inputs:

  1. Company + customer context (latest ContextBrief per scope).
  2. Per-competitor stanzas: category, threat angle, latest CompetitorReport
     excerpt, latest DeepResearchReport excerpt.
  3. Findings digest: every Finding in the last N days, grouped by competitor,
     newest first, capped per competitor so one noisy competitor can't crowd
     out the rest.
  4. Deep-research digest: a compact cross-competitor index of what the
     existing per-competitor Gemini dossiers cover, so the synthesis can
     reference them without re-reading the full bodies.

Returns the filled skill template plus an `inputs_meta` dict written into
`MarketSynthesisReport.inputs_meta` so a thin synthesis is diagnosable at a
glance on the detail page.

Pure function — no DB writes, no network, no Gemini calls. Caller owns the
job wrapper.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from .models import (
    Competitor,
    CompetitorReport,
    ContextBrief,
    DeepResearchReport,
    Finding,
)
from .skills import load_active


# Per-competitor caps. Rough upper bound at 20 active competitors is ~240KB
# of brief once you include reviews + DR excerpts; going much higher risks
# token budgets on Gemini's side for diminishing synthesis quality.
MAX_FINDINGS_PER_COMPETITOR = 20
REVIEW_EXCERPT_CHARS = 2000
DR_EXCERPT_CHARS = 2000
# Hard cap on total composed brief. When exceeded, the composer drops the
# oldest findings first and records the truncation in inputs_meta so we can
# see it on the detail page.
MAX_BRIEF_CHARS = 500_000


def _load_config() -> dict:
    """Read config.json directly — service.load_config aborts on missing
    ANTHROPIC_API_KEY at import time and we don't need Anthropic here."""
    path = os.environ.get("CONFIG_PATH", "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _truncate(text: str, limit: int) -> str:
    """Character-bounded truncation with an ellipsis marker when we cut.
    Empty strings pass through unchanged."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …[truncated]"


def _latest_context(db: Session, scope: str) -> str:
    """Latest ContextBrief body for a given scope, or '(not available)'.
    Missing briefs are non-fatal — the synthesis just runs without that
    grounding and the job wrapper logs a warn RunEvent."""
    row = (
        db.query(ContextBrief)
        .filter(ContextBrief.scope == scope)
        .order_by(ContextBrief.created_at.desc())
        .first()
    )
    if row and row.body_md:
        return row.body_md.strip()
    return "(not available)"


def _latest_review(db: Session, competitor_id: int) -> CompetitorReport | None:
    return (
        db.query(CompetitorReport)
        .filter(CompetitorReport.competitor_id == competitor_id)
        .order_by(CompetitorReport.created_at.desc())
        .first()
    )


def _latest_dr(db: Session, competitor_id: int) -> DeepResearchReport | None:
    """Latest *ready* deep-research report — queued/running/failed rows
    aren't grounding, they're in-flight state."""
    return (
        db.query(DeepResearchReport)
        .filter(
            DeepResearchReport.competitor_id == competitor_id,
            DeepResearchReport.status == "ready",
        )
        .order_by(DeepResearchReport.started_at.desc())
        .first()
    )


def _findings_for(
    db: Session, competitor_name: str, since: datetime, cap: int
) -> list[Finding]:
    """Most-recent-first findings for one competitor within the window,
    capped. Uses published_at when available, created_at otherwise — the
    same 'effective date' semantics the stream UI uses."""
    return (
        db.query(Finding)
        .filter(
            Finding.competitor == competitor_name,
            Finding.created_at >= since,
        )
        .order_by(Finding.created_at.desc())
        .limit(cap)
        .all()
    )


def _format_finding(f: Finding) -> str:
    """One-line markdown bullet. Keeps the brief skimmable for Gemini —
    never inlines raw URLs (Gemini cites its own sources; ours are for
    grounding, not for it to follow)."""
    when = f.published_at or f.created_at
    date_str = when.strftime("%Y-%m-%d") if when else "unknown"
    signal = f.signal_type or "signal"
    threat = f.digest_threat_level or "–"
    body = (f.summary or f.title or f.content or "").strip().replace("\n", " ")
    if len(body) > 400:
        body = body[:400].rstrip() + "…"
    return f"- {date_str} · {signal} · {threat} — {body}"


def _competitor_stanza(
    competitor: Competitor,
    review: CompetitorReport | None,
    dr: DeepResearchReport | None,
) -> str:
    """Per-competitor context block. Header + threat angle + review excerpt
    + DR excerpt. Each excerpt is capped so one chatty competitor doesn't
    dominate the composed brief."""
    lines: list[str] = [f"### {competitor.name} ({competitor.category or 'uncategorised'})"]
    if competitor.threat_angle:
        lines.append(f"Threat angle: {competitor.threat_angle}")
    if review and review.body_md:
        lines.append(
            f"Latest strategy review ({review.created_at:%Y-%m-%d}):\n"
            + _truncate(review.body_md.strip(), REVIEW_EXCERPT_CHARS)
        )
    else:
        lines.append("Latest strategy review: (none yet)")
    if dr and dr.body_md:
        lines.append(
            f"Latest deep-research ({dr.started_at:%Y-%m-%d}, "
            f"{len(dr.sources or [])} sources):\n"
            + _truncate(dr.body_md.strip(), DR_EXCERPT_CHARS)
        )
    else:
        lines.append("Latest deep-research: (none yet)")
    return "\n\n".join(lines)


def _dr_digest_line(
    competitor: Competitor, dr: DeepResearchReport | None
) -> str | None:
    """One-line cross-competitor index entry for the deep-research digest
    section. None when the competitor has no ready DR report yet."""
    if dr is None or not dr.body_md:
        return None
    first_line = ""
    for line in dr.body_md.splitlines():
        s = line.strip()
        if s:
            first_line = s
            break
    # Strip markdown heading markers for a cleaner index listing.
    first_line = first_line.lstrip("# ").strip()
    if len(first_line) > 180:
        first_line = first_line[:180].rstrip() + "…"
    return (
        f"- **{competitor.name}** ({dr.started_at:%Y-%m-%d}, "
        f"{len(dr.sources or [])} sources): {first_line}"
    )


def compose_brief(
    db: Session, *, window_days: int = 30
) -> tuple[str, dict[str, Any]]:
    """Build the synthesis brief. Returns `(brief_text, inputs_meta)`.

    `inputs_meta` shape:
        {
          "findings_count": int,       # total findings included
          "competitors_covered": int,  # active competitors in the brief
          "dr_reports_used": int,      # competitors with a ready DR excerpt
          "brief_chars": int,          # len(brief_text) — watch the cap
          "window_days": int,          # echo of the arg, for the UI
          "truncated": bool,           # True when MAX_BRIEF_CHARS hit
          "missing_context": list[str],# scopes with no ContextBrief
        }
    """
    config = _load_config()
    our_company = config.get("company", "Seek")
    our_industry = config.get("industry", "job search and recruitment platforms")

    since = datetime.utcnow() - timedelta(days=max(1, int(window_days)))

    company_brief = _latest_context(db, "company")
    customer_brief = _latest_context(db, "customer")
    missing_context = [
        scope for scope, body in (("company", company_brief), ("customer", customer_brief))
        if body == "(not available)"
    ]

    active = (
        db.query(Competitor)
        .filter(Competitor.active == True)  # noqa: E712
        .order_by(Competitor.name)
        .all()
    )

    stanzas: list[str] = []
    findings_sections: list[str] = []
    dr_lines: list[str] = []
    total_findings = 0
    dr_used = 0

    for comp in active:
        review = _latest_review(db, comp.id)
        dr = _latest_dr(db, comp.id)
        if dr is not None:
            dr_used += 1
            dr_line = _dr_digest_line(comp, dr)
            if dr_line:
                dr_lines.append(dr_line)

        stanzas.append(_competitor_stanza(comp, review, dr))

        rows = _findings_for(db, comp.name, since, MAX_FINDINGS_PER_COMPETITOR)
        if rows:
            total_findings += len(rows)
            formatted = "\n".join(_format_finding(r) for r in rows)
            findings_sections.append(f"### {comp.name}\n{formatted}")

    competitor_context = (
        "\n\n".join(stanzas) if stanzas else "(no active competitors)"
    )
    findings_digest = (
        "\n\n".join(findings_sections)
        if findings_sections
        else "(no findings in window)"
    )
    deep_research_digest = (
        "\n".join(dr_lines)
        if dr_lines
        else "(no deep-research dossiers available yet)"
    )

    template = load_active("market_synthesis_brief") or ""
    brief = template
    for k, v in {
        "our_company": our_company,
        "our_industry": our_industry,
        "window_days": str(window_days),
        "company_brief": company_brief,
        "customer_brief": customer_brief,
        "competitor_context": competitor_context,
        "findings_digest": findings_digest,
        "deep_research_digest": deep_research_digest,
    }.items():
        brief = brief.replace("{{" + k + "}}", v)

    truncated = False
    if len(brief) > MAX_BRIEF_CHARS:
        # Drop findings first (they're the largest and most expendable —
        # reviews and DR excerpts carry more per-char value). Rebuild the
        # findings section with a shrinking cap until we fit.
        cap = MAX_FINDINGS_PER_COMPETITOR
        while len(brief) > MAX_BRIEF_CHARS and cap > 1:
            cap = max(1, cap // 2)
            total_findings = 0
            findings_sections = []
            for comp in active:
                rows = _findings_for(db, comp.name, since, cap)
                if rows:
                    total_findings += len(rows)
                    formatted = "\n".join(_format_finding(r) for r in rows)
                    findings_sections.append(f"### {comp.name}\n{formatted}")
            findings_digest = (
                "\n\n".join(findings_sections)
                if findings_sections
                else "(no findings in window)"
            )
            brief = template
            for k, v in {
                "our_company": our_company,
                "our_industry": our_industry,
                "window_days": str(window_days),
                "company_brief": company_brief,
                "customer_brief": customer_brief,
                "competitor_context": competitor_context,
                "findings_digest": findings_digest,
                "deep_research_digest": deep_research_digest,
            }.items():
                brief = brief.replace("{{" + k + "}}", v)
        truncated = True

    inputs_meta: dict[str, Any] = {
        "findings_count": total_findings,
        "competitors_covered": len(active),
        "dr_reports_used": dr_used,
        "brief_chars": len(brief),
        "window_days": int(window_days),
        "truncated": truncated,
        "missing_context": missing_context,
    }
    return brief, inputs_meta
