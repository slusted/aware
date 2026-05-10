"""Market-wide product releases brief.

A cross-competitor read of what shipped across all tracked competitors in
a recent window. Distinct from the daily market digest (which spans every
signal type) and from the per-competitor strategy review.

Pipeline:
  1. Pull all `product_launch` findings in the window across active
     competitors.
  2. Pull `messaging_shift` findings too — homepage/positioning rewrites
     that are *actually* describing a launched feature classify here, and
     the LLM filters them in step 3 (the model sees the source/title and
     drops anything that's pure narrative repositioning).
  3. One Haiku call clusters the pool into product themes, drafts a
     top-line read, lists each release as a bullet under its theme, and
     calls out quiet competitors. Theme labels come from the data, not a
     fixed taxonomy — that's the whole point of the tab.
  4. Persist as a Report row with kind="market_releases".

No materiality threshold (deliberate — the user wants the long tail to
see where the market is *focused*, not just where it's loud). No
per-competitor cap either; both are knobs we can add once we see real
output.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from .models import Competitor, Finding, Report


MODEL = "claude-haiku-4-5-20251001"
DEFAULT_WINDOW_DAYS = 30
_MAX_FINDING_CHARS = 1200      # per finding, fed to the model
_MAX_FINDINGS_INPUT = 400      # hard ceiling so a runaway window never blows the prompt


_SYSTEM_PROMPT = """You are a competitive-intelligence analyst writing a cross-market product-releases brief for a strategy team.

Input: a list of findings the upstream classifier flagged as either `product_launch` or `messaging_shift`. Each line carries the competitor name, date, source, signal_type, title, URL, and a one-line summary. Some `messaging_shift` rows are pure positioning rewrites with no real launch behind them — drop those. Keep `messaging_shift` rows only when they describe a concrete shipped feature/product/capability.

Your output is a single Markdown document with this exact structure:

## Top-line read

2–3 sentences on where the market is focused this window. Name the dominant themes, the intensity, and any notable absence. No hedging, no preamble like "This brief shows…".

## By theme

Cluster the kept releases into 4–8 product themes derived from the data itself (NOT a fixed taxonomy). Theme names should be specific enough that a reader can tell what they're about — "Agentic workflows", "Pricing & packaging", "Enterprise security & compliance", "Mobile experience", "Recruiter productivity", "Data & analytics", "Marketplace integrations", etc. If a release straddles two themes, place it under the dominant one.

For each theme, use this format:

### {Theme name} ({N} releases)

One short sentence framing what's in this bucket and why it matters.

- **{Competitor}** ({YYYY-MM-DD}) — {plain-language description of what shipped, ≤200 chars}. [link]({url})
- **{Competitor}** ({YYYY-MM-DD}) — …

One bullet per release. Sort bullets newest first inside each theme. If two releases from the same competitor land in the same theme, keep both — duplication is a signal.

## Cross-cutting observations

A short bulleted list (3–6 points) of patterns that span themes: who's converging on the same idea, who's the outlier, what gap nobody is filling, what last quarter's noise has gone quiet. This is the section that earns the tab's existence — make it sharp.

## Quiet competitors

A bulleted list of tracked competitors with zero releases in the window (the caller will provide the full list). One line each: "**{Competitor}** — no product launches in window." Useful negative signal; do not invent reasons.

## Rules

- One bullet = one release. Never merge two competitors into a single bullet.
- If you drop a `messaging_shift` row, do not mention it.
- No fabrication: every bullet must trace to an input finding.
- No exec summary, no closing paragraph, no "in conclusion" tail.
- Plain prose, no marketing language ("revolutionary", "next-gen", etc.).
"""


def _gather_findings(db: Session, days: int) -> list[Finding]:
    since = datetime.utcnow() - timedelta(days=days)
    return (
        db.query(Finding)
        .filter(Finding.created_at >= since)
        .filter(Finding.signal_type.in_(["product_launch", "messaging_shift"]))
        .order_by(Finding.created_at.desc())
        .limit(_MAX_FINDINGS_INPUT)
        .all()
    )


def _gather_active_competitors(db: Session) -> list[str]:
    rows = (
        db.query(Competitor)
        .filter(Competitor.active == True)  # noqa: E712
        .order_by(Competitor.name)
        .all()
    )
    return [c.name for c in rows]


def _format_finding_line(f: Finding) -> str:
    when = f.created_at.strftime("%Y-%m-%d") if f.created_at else "????-??-??"
    title = (f.title or "").strip().replace("\n", " ")
    summary = (f.summary or f.content or "").strip().replace("\n", " ")
    if len(summary) > _MAX_FINDING_CHARS:
        summary = summary[:_MAX_FINDING_CHARS] + "…"
    url = f.url or ""
    return (
        f"- competitor={f.competitor!r} date={when} signal_type={f.signal_type} "
        f"source={f.source} url={url}\n"
        f"  title: {title}\n"
        f"  summary: {summary}"
    )


def _build_user_prompt(
    days: int, findings: list[Finding], all_competitors: list[str], company: str
) -> str:
    if not findings:
        finding_block = "(no product_launch or messaging_shift findings in this window)"
    else:
        finding_block = "\n".join(_format_finding_line(f) for f in findings)

    competitor_block = "\n".join(f"- {n}" for n in all_competitors) or "(no active competitors configured)"

    return f"""# Cross-market product-releases brief

Reader: the {company} strategy team.

Window: last {days} days (until {datetime.utcnow():%Y-%m-%d}).

## Tracked competitors (active)
{competitor_block}

## Findings to cluster ({len(findings)} rows)

{finding_block}

---

Produce the brief now, exactly in the structure described in the system prompt.
If there are zero findings to cluster, output the headings with a single line under each that explicitly says no releases were captured in this window — do not invent any.
"""


def synthesize_releases(
    db: Session,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    run_id: int | None = None,
    company: str = "Seek",
) -> Report:
    """Generate one releases brief over the last `days` days. Always writes
    a Report (even on empty input — the empty-state output is informative
    on its own and trivially cheap)."""
    findings = _gather_findings(db, days)
    competitors = _gather_active_competitors(db)

    # Reuse the analyzer's anthropic.Client so we share API key + retry
    # config + any future prompt-cache wiring with the rest of the app.
    import analyzer
    client = analyzer.client

    user = _build_user_prompt(days, findings, competitors, company)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    body = resp.content[0].text

    title = f"Product releases · last {days}d · {datetime.utcnow():%Y-%m-%d %H:%M}"
    report = Report(
        run_id=run_id,
        kind="market_releases",
        title=title,
        body_md=body,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report
