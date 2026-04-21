"""Positioning-pillar extraction pipeline.

Two-call Haiku flow per competitor:

  1. `_extract_pillars(text)`       marketing pages → pillars JSON
  2. `_write_narrative(pillars, prior)`  pillars + prior → markdown body

Both prompts live under `skill/` and are loaded via `app.skills.load_active`
so they're editable at /settings/skills.

Entry point: `extract_positioning(competitor, db)` — called by the monthly
scheduler job and the manual "Refresh positioning" button on the
competitor profile. Returns the snapshot that represents the current view
(either a freshly written row or the existing latest one if the source
pages haven't changed).

Fail soft:
  - Fetch returns nothing usable → return None, no row written.
  - Extract call malformed → raise; caller isolates per competitor.
  - Narrative call fails after extract succeeded → write the snapshot with
    empty `body_md` (pillars alone are still useful). The UI renders a
    muted "Narrative unavailable" line.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import Competitor, PositioningSnapshot
from .. import fetcher
from .. import skills


MODEL = "claude-haiku-4-5"

# Cap the concatenated marketing text before it hits the LLM. Marketing
# copy value is front-loaded; truncate the tail.
_MAX_INPUT_CHARS = 40_000

# Default pages to probe when competitor.positioning_pages is empty.
# Relative to the homepage domain.
_AUTO_PATHS = ("", "/pricing", "/plans", "/product", "/features")


# ────────────────────────────────────────────────────────────────
# Fetching

def _urls_for(competitor: Competitor) -> list[str]:
    """Resolve the list of URLs to fetch for this competitor. Returns
    [] when there's nothing usable (no homepage_domain and no overrides)."""
    overrides = list(competitor.positioning_pages or [])
    if overrides:
        return [u for u in overrides if u]
    domain = (competitor.homepage_domain or "").strip().lower()
    if not domain:
        return []
    # Strip any accidental scheme/www prefix.
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    domain = domain.strip("/").removeprefix("www.")
    return [f"https://{domain}{p}" for p in _AUTO_PATHS]


def _fetch_pages(urls: list[str]) -> tuple[str, list[str]]:
    """Fetch each URL through the project's existing fetcher and concatenate
    the usable results with `--- {url} ---` separators.

    Returns (concatenated_text, fetched_urls). `fetched_urls` is the
    subset that came back non-empty — stored on the snapshot so a future
    refetch knows which pages actually worked.
    """
    chunks: list[str] = []
    fetched: list[str] = []
    total = 0
    for url in urls:
        try:
            content, _source = fetcher.fetch_article(url)
        except Exception as e:
            print(f"[positioning] fetch failed for {url}: {e}")
            continue
        if not content or not content.strip():
            continue
        body = content.strip()
        # Leave headroom for separators; never allow one page to eat the
        # whole budget.
        remaining = _MAX_INPUT_CHARS - total
        if remaining <= 200:
            break
        if len(body) > remaining - 200:
            body = body[: remaining - 200]
        chunk = f"--- {url} ---\n{body}"
        chunks.append(chunk)
        fetched.append(url)
        total += len(chunk) + 2  # +separator
    return ("\n\n".join(chunks), fetched)


# ────────────────────────────────────────────────────────────────
# LLM calls

def _load_client():
    """Reuse the instrumented client so calls get logged to usage_events."""
    import analyzer
    return analyzer.client


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _sanitize_pillars(raw: Any) -> list[dict]:
    """Validate and normalize the pillars payload from the extract call.
    Returns a list of well-shaped dicts; drops any malformed entries."""
    if not isinstance(raw, list):
        return []
    cleaned: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        quote = str(item.get("quote") or "").strip()
        source_url = str(item.get("source_url") or "").strip()
        try:
            weight = int(item.get("weight") or 0)
        except (TypeError, ValueError):
            weight = 0
        weight = max(1, min(5, weight))
        if not name:
            continue
        if len(name) > 80:
            name = name[:80].rstrip()
        if len(quote) > 200:
            quote = quote[:199].rstrip() + "…"
        cleaned.append({
            "name": name,
            "weight": weight,
            "quote": quote,
            "source_url": source_url,
        })
    return cleaned[:6]


def _extract_pillars(competitor: Competitor, text: str) -> list[dict]:
    """Haiku call 1: marketing pages → pillars JSON. Raises on malformed
    response; caller catches + skips the snapshot."""
    system = skills.load_active("positioning_extract") or ""
    if not system:
        raise RuntimeError("skill 'positioning_extract' not found")

    user = (
        f"Competitor: {competitor.name}\n"
        f"Category: {competitor.category or 'unspecified'}\n\n"
        f"{text}"
    )

    client = _load_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user}],
    )
    raw = resp.content[0].text if resp.content else ""
    parsed = _parse_json(raw)
    if not parsed or "pillars" not in parsed:
        raise ValueError(
            f"positioning_extract returned no pillars. Raw: {raw[:500]}"
        )
    return _sanitize_pillars(parsed.get("pillars"))


def _write_narrative(
    competitor: Competitor,
    pillars: list[dict],
    prior: PositioningSnapshot | None,
) -> str:
    """Haiku call 2: pillars + prior pillars → markdown body. Returns ""
    on any error — the snapshot is still written with the pillars."""
    system = skills.load_active("positioning_narrative") or ""
    if not system:
        print("[positioning] skill 'positioning_narrative' not found; "
              "skipping narrative")
        return ""

    prior_block = "(no prior snapshot)"
    if prior is not None:
        prior_block = (
            f"Prior snapshot date: {prior.created_at.strftime('%Y-%m-%d')}\n"
            f"Prior pillars:\n{json.dumps(prior.pillars or [], indent=2)}"
        )

    user = (
        f"Competitor: {competitor.name}\n\n"
        f"# Current pillars\n{json.dumps(pillars, indent=2)}\n\n"
        f"# Prior\n{prior_block}\n"
    )

    try:
        client = _load_client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return (resp.content[0].text if resp.content else "").strip()
    except Exception as e:
        print(f"[positioning] narrative call failed for {competitor.name}: {e}")
        return ""


# ────────────────────────────────────────────────────────────────
# Entry point

def _latest_snapshot(db: Session, competitor_id: int) -> PositioningSnapshot | None:
    return (
        db.query(PositioningSnapshot)
        .filter(PositioningSnapshot.competitor_id == competitor_id)
        .order_by(PositioningSnapshot.created_at.desc())
        .first()
    )


def extract_positioning(
    competitor: Competitor,
    db: Session,
) -> PositioningSnapshot | None:
    """Fetch the competitor's marketing pages, extract pillars, write the
    narrative, persist a snapshot. Returns the snapshot that represents
    the current view (new row on success, existing latest on short-circuit,
    None when there's nothing usable to fetch).
    """
    urls = _urls_for(competitor)
    if not urls:
        print(f"[positioning] no homepage_domain or override for {competitor.name}")
        return None

    text, fetched_urls = _fetch_pages(urls)
    if not text.strip() or not fetched_urls:
        print(f"[positioning] no usable content fetched for {competitor.name}")
        return None

    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    prior = _latest_snapshot(db, competitor.id)
    if prior is not None and prior.source_hash == source_hash:
        print(f"[positioning] source unchanged for {competitor.name}, "
              f"keeping snapshot #{prior.id}")
        return prior

    pillars = _extract_pillars(competitor, text)
    body_md = _write_narrative(competitor, pillars, prior)

    snap = PositioningSnapshot(
        competitor_id=competitor.id,
        pillars=pillars,
        body_md=body_md,
        source_urls=fetched_urls,
        source_hash=source_hash,
        model=MODEL,
        created_at=datetime.utcnow(),
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    print(f"[positioning] wrote snapshot #{snap.id} for {competitor.name} "
          f"({len(pillars)} pillars, narrative={'yes' if body_md else 'no'})")
    return snap


def refresh_all_active(db: Session) -> dict:
    """Iterate active competitors and extract positioning for each.
    Failures per competitor are isolated. Used by the monthly scheduler job.
    """
    active = (
        db.query(Competitor)
        .filter(Competitor.active == True)
        .order_by(Competitor.name)
        .all()
    )
    summary = {"snapshots": [], "unchanged": [], "skipped": [], "errors": []}
    for c in active:
        try:
            snap = extract_positioning(c, db)
        except Exception as e:
            summary["errors"].append({"competitor": c.name, "error": str(e)})
            continue
        if snap is None:
            summary["skipped"].append(c.name)
            continue
        # Was this a new row or the short-circuited prior?
        latest = _latest_snapshot(db, c.id)
        if latest and latest.id == snap.id and (
            (datetime.utcnow() - snap.created_at).total_seconds() < 300
        ):
            summary["snapshots"].append(c.name)
        else:
            summary["unchanged"].append(c.name)
    return summary
