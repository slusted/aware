"""Market-wide hiring brief.

A cross-market read of what every tracked competitor is recruiting for
in a recent window. The headline cut is by Competitor.category
(job_board / ats / labour_hire / adjacent / …) — the user's question
isn't "what is each competitor doing" but "what does each *type* of
competitor's hiring tell us." Per-competitor depth lives elsewhere.

Filter scope: postings only.
  - signal_type = "new_hire"
  - AND (source = "careers" OR topic = "strategic hiring")
This excludes named-hire announcements (the other half of the new_hire
classifier — see app/signals/llm_classify.py rubric) since those are
"people moves," a different question.

Pipeline is two-step because hiring volume is much higher than product
launches:
  1. Per-competitor mini-summary (parallelisable Haiku calls). Input:
     that competitor's capped postings + their existing strategy review
     (CompetitorReport.body_md). Output: a structured JSON blob with
     counts, generic role mix, themes, and locations.
  2. Cross-stitch (single Haiku call). Input: all per-competitor JSONs
     grouped by Competitor.category, plus the list of quiet competitors.
     Output: the markdown brief.

No raw posting titles in the final brief — the user wants generic role
descriptions ("senior backend engineers", "enterprise AEs") so the read
stays at category level.

Caps:
  - HIRING_PER_COMPETITOR_CAP (env, default 50): top-N most-recent
    postings per competitor.
  - HIRING_INPUT_HARD_CEILING (env, default 5000): total postings
    across the input. If cap × competitors would exceed this, oldest
    rows are trimmed first.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import Competitor, CompetitorReport, Finding, Report


MODEL = "claude-haiku-4-5-20251001"
DEFAULT_WINDOW_DAYS = 30
DEFAULT_PER_COMPETITOR_CAP = 50
DEFAULT_INPUT_HARD_CEILING = 5000

# Per-competitor step inputs. Keep the role context tight — the model
# only needs role title + url to classify; the long content blob hurts
# more than it helps and inflates token cost.
_MAX_TITLE_CHARS = 240
_MAX_CONTENT_CHARS = 600


def _per_competitor_cap() -> int:
    try:
        return max(1, int(os.environ.get("HIRING_PER_COMPETITOR_CAP", DEFAULT_PER_COMPETITOR_CAP)))
    except (TypeError, ValueError):
        return DEFAULT_PER_COMPETITOR_CAP


def _input_hard_ceiling() -> int:
    try:
        return max(1, int(os.environ.get("HIRING_INPUT_HARD_CEILING", DEFAULT_INPUT_HARD_CEILING)))
    except (TypeError, ValueError):
        return DEFAULT_INPUT_HARD_CEILING


@dataclass
class _CompetitorCut:
    """One competitor's slice of input data heading into step 1."""
    competitor_id: int
    name: str
    category: str
    postings: list[Finding] = field(default_factory=list)
    strategy_review: str = ""


# ──────────────────────────────────────────────────────────────────
# Data gathering
# ──────────────────────────────────────────────────────────────────

def _gather_postings(db: Session, days: int) -> list[Finding]:
    since = datetime.utcnow() - timedelta(days=days)
    return (
        db.query(Finding)
        .filter(Finding.created_at >= since)
        .filter(Finding.signal_type == "new_hire")
        # Postings only — exclude named-hire announcements. The
        # classifier sets source="careers" for ATS/careers-page hits and
        # topic="strategic hiring" for the hiring-sweep findings.
        .filter((Finding.source == "careers") | (Finding.topic == "strategic hiring"))
        .order_by(Finding.created_at.desc())
        .all()
    )


def _gather_active_competitors(db: Session) -> list[Competitor]:
    return (
        db.query(Competitor)
        .filter(Competitor.active == True)  # noqa: E712
        .order_by(Competitor.name)
        .all()
    )


def _latest_strategy_review(db: Session, competitor_id: int) -> str:
    row = (
        db.query(CompetitorReport)
        .filter(CompetitorReport.competitor_id == competitor_id)
        .order_by(CompetitorReport.created_at.desc())
        .first()
    )
    return (row.body_md or "") if row else ""


def _bucket_and_cap(
    postings: list[Finding],
    competitors: list[Competitor],
    *,
    per_competitor_cap: int,
    hard_ceiling: int,
) -> tuple[list[_CompetitorCut], list[str], int, int]:
    """Group postings by competitor name → CompetitorCut. Apply per-competitor
    cap (top-N most recent) then enforce the global hard ceiling by trimming
    oldest rows across the *combined* pool until under the ceiling.

    Returns (cuts, quiet_competitor_names, kept_count, dropped_count).
    """
    by_name = {c.name: c for c in competitors}

    grouped: dict[str, list[Finding]] = {}
    for f in postings:
        if f.competitor in by_name:
            grouped.setdefault(f.competitor, []).append(f)
        # Skip postings whose competitor isn't in the active roster — they
        # came from a now-deactivated competitor and shouldn't sway the
        # read on what the *current* market is doing.

    # Per-competitor cap (postings already sorted desc by created_at).
    capped: dict[str, list[Finding]] = {}
    for name, rows in grouped.items():
        capped[name] = rows[:per_competitor_cap]

    # Hard ceiling: if the combined pool exceeds the ceiling, trim oldest
    # rows globally. This is a safety net — at default 5000 it almost
    # never fires unless a window of years was requested.
    total = sum(len(rs) for rs in capped.values())
    dropped = 0
    if total > hard_ceiling:
        # Flatten with (created_at, name, finding), sort newest-first, slice.
        flat = [
            (f.created_at or datetime.min, name, f)
            for name, rows in capped.items() for f in rows
        ]
        flat.sort(reverse=True, key=lambda t: t[0])
        kept_flat = flat[:hard_ceiling]
        dropped = total - hard_ceiling
        capped = {}
        for _, name, f in kept_flat:
            capped.setdefault(name, []).append(f)

    cuts: list[_CompetitorCut] = []
    quiet: list[str] = []
    for c in competitors:
        rows = capped.get(c.name, [])
        if rows:
            cuts.append(_CompetitorCut(
                competitor_id=c.id,
                name=c.name,
                category=c.category or "uncategorised",
                postings=rows,
            ))
        else:
            quiet.append(c.name)

    kept = sum(len(c.postings) for c in cuts)
    return cuts, quiet, kept, dropped


# ──────────────────────────────────────────────────────────────────
# Step 1 — per-competitor mini-summary
# ──────────────────────────────────────────────────────────────────

# Both system prompts live in the skill system so analysts can edit them
# via /settings/skills without a code change. Seeds in skill/. We resolve
# both bodies once at the start of synthesize_hiring and pass them in to
# avoid a DB hit per parallel per-competitor call.
SKILL_PER_COMPETITOR = "market_hiring_per_competitor"
SKILL_STITCH = "market_hiring_stitch"


def _format_posting_for_step1(f: Finding) -> str:
    title = (f.title or "").strip().replace("\n", " ")[:_MAX_TITLE_CHARS]
    content = (f.content or f.summary or "").strip().replace("\n", " ")[:_MAX_CONTENT_CHARS]
    when = f.created_at.strftime("%Y-%m-%d") if f.created_at else "????-??-??"
    return f"- [{when}] {title} :: {content}"


def _per_competitor_user_prompt(cut: _CompetitorCut) -> str:
    posts = "\n".join(_format_posting_for_step1(f) for f in cut.postings)
    review = (cut.strategy_review or "").strip()
    review_block = (
        f"## Existing strategy review\n{review[:6000]}\n"
        if review
        else "## Existing strategy review\n(none on file)\n"
    )
    return f"""# Competitor: {cut.name}
# Category: {cut.category}

{review_block}
## Open postings ({len(cut.postings)})
{posts}

Produce the JSON snapshot now.
"""


def _classify_competitor(client, cut: _CompetitorCut, system_prompt: str) -> dict:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=900,
        system=system_prompt,
        messages=[{"role": "user", "content": _per_competitor_user_prompt(cut)}],
    )
    raw = resp.content[0].text.strip()
    # Strip code fences if the model added them despite instructions.
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
        # Drop any trailing closing fence that survived.
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    # Tolerant {} extraction.
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]
    snap = json.loads(raw)
    # Stamp identity fields the model might have skipped or hallucinated.
    snap["name"] = cut.name
    snap["category"] = cut.category
    snap["count"] = len(cut.postings)
    return snap


# ──────────────────────────────────────────────────────────────────
# Step 2 — cross-stitch
# ──────────────────────────────────────────────────────────────────

# System prompt lives in the skill system; seed at skill/market_hiring_stitch.md.


def _format_snapshots_for_stitch(snapshots: list[dict]) -> str:
    """Group snapshots by category and emit one block per category, with the
    full structured snapshots inside. The model needs the structure visible
    so it can count and compare — markdown bullets would lose that fidelity.
    """
    by_cat: dict[str, list[dict]] = {}
    for s in snapshots:
        by_cat.setdefault(s.get("category", "uncategorised"), []).append(s)

    parts: list[str] = []
    for cat in sorted(by_cat.keys()):
        items = by_cat[cat]
        total = sum(int(s.get("count", 0)) for s in items)
        parts.append(f"## category: {cat}  (total postings: {total}, competitors: {len(items)})")
        for s in items:
            parts.append(json.dumps(s, indent=2, ensure_ascii=False))
    return "\n\n".join(parts) if parts else "(no snapshots — every active competitor was quiet)"


def _stitch_user_prompt(
    days: int,
    snapshots: list[dict],
    quiet_competitors: list[tuple[str, str]],
    company: str,
    total_postings: int,
    dropped_for_ceiling: int,
) -> str:
    quiet_block = "\n".join(
        f"- {name} ({category})" for name, category in quiet_competitors
    ) or "(none — every active competitor had at least one posting)"

    ceiling_note = ""
    if dropped_for_ceiling > 0:
        ceiling_note = (
            f"\nNote: {dropped_for_ceiling} of the oldest postings were "
            f"trimmed to keep the input under the configured hard ceiling. "
            f"Ignore this in your brief — it's a plumbing detail, not a signal.\n"
        )

    return f"""# Cross-market hiring brief

Reader: the {company} strategy team.

Window: last {days} days (until {datetime.utcnow():%Y-%m-%d}).
Total postings considered: {total_postings}.{ceiling_note}

## Per-competitor snapshots, grouped by category

{_format_snapshots_for_stitch(snapshots)}

## Quiet competitors (active, but zero postings in window)
{quiet_block}

---

Produce the brief now, exactly in the structure the system prompt describes.
If there are zero snapshots, output every required section with a single
line under each that explicitly says no postings were captured.
"""


# ──────────────────────────────────────────────────────────────────
# Public entrypoint
# ──────────────────────────────────────────────────────────────────

def synthesize_hiring(
    db: Session,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    run_id: int | None = None,
    company: str = "Seek",
    log=print,
) -> Report:
    """Generate one hiring brief over the last `days` days. Always writes a
    Report, even on empty input."""
    competitors = _gather_active_competitors(db)
    postings = _gather_postings(db, days)

    cuts, quiet_names, kept, dropped = _bucket_and_cap(
        postings,
        competitors,
        per_competitor_cap=_per_competitor_cap(),
        hard_ceiling=_input_hard_ceiling(),
    )
    log(f"[hiring] {len(postings)} postings → {kept} kept across "
        f"{len(cuts)} competitors (dropped {dropped} for ceiling); "
        f"{len(quiet_names)} quiet")

    # Hydrate strategy reviews — separate query per competitor since these
    # are append-only "latest wins" tables and the row count is tiny.
    for cut in cuts:
        cut.strategy_review = _latest_strategy_review(db, cut.competitor_id)

    import analyzer
    client = analyzer.client

    # Resolve both skill bodies once. Avoids a DB hit per parallel call
    # in the fan-out below.
    from .skills import load_active
    per_competitor_system = load_active(SKILL_PER_COMPETITOR)
    stitch_system = load_active(SKILL_STITCH)

    # Step 1 — fan out per-competitor calls. Concurrency kept modest so we
    # don't hammer rate limits on a 30-competitor org.
    snapshots: list[dict] = []
    if cuts:
        max_workers = min(8, len(cuts))
        log(f"[hiring] step 1: fanning out {len(cuts)} per-competitor calls "
            f"(max_workers={max_workers})")
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(_classify_competitor, client, c, per_competitor_system): c
                for c in cuts
            }
            for fut in as_completed(futures):
                cut = futures[fut]
                try:
                    snapshots.append(fut.result())
                except Exception as e:
                    # Don't fail the whole brief over one classification miss —
                    # emit a minimal snapshot so the count is still accurate.
                    log(f"[hiring] step 1 failed for {cut.name}: {e}")
                    snapshots.append({
                        "name": cut.name,
                        "category": cut.category,
                        "count": len(cut.postings),
                        "by_function": {},
                        "by_seniority": {},
                        "themes": [],
                        "locations": [],
                        "common_roles": [],
                        "unusual_roles": [],
                        "strategic_read": "(snapshot generation failed)",
                    })

    # Pair each quiet competitor with its category for the brief.
    name_to_category = {c.name: (c.category or "uncategorised") for c in competitors}
    quiet_pairs = [(n, name_to_category.get(n, "uncategorised")) for n in quiet_names]

    # Step 2 — cross-stitch.
    log(f"[hiring] step 2: cross-stitch over {len(snapshots)} snapshots")
    user = _stitch_user_prompt(
        days=days,
        snapshots=snapshots,
        quiet_competitors=quiet_pairs,
        company=company,
        total_postings=kept,
        dropped_for_ceiling=dropped,
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=stitch_system,
        messages=[{"role": "user", "content": user}],
    )
    body = resp.content[0].text

    title = f"Hiring brief · last {days}d · {datetime.utcnow():%Y-%m-%d %H:%M}"
    report = Report(
        run_id=run_id,
        kind="market_hiring",
        title=title,
        body_md=body,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report
