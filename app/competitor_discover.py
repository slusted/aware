"""Competitor discovery — mines the last 90 days of findings for company
names mentioned but not yet on the watchlist.

The previous implementation drove an Anthropic tool-use loop with web
search exposed; it was slow, expensive, and frequently surfaced nothing
new because cold speculation rarely beats what's already in our corpus.

The current implementation is single-shot: load high-materiality findings
from the recent window, hand the title+snippet of each to Claude, and ask
it to surface companies mentioned by name that we should consider
tracking. Each candidate cites the finding ids that mentioned it; we
turn those back into clickable evidence chips on the panel.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Iterator, Iterable

import anthropic
from sqlalchemy import desc
from sqlalchemy.orm import Session

from .competitor_autofill import _extract_json
from .models import Finding
from .skills import load_active

MODEL = "claude-sonnet-4-6"
MAX_CANDIDATES = 12
LOOKBACK_DAYS = 90
# Cap how many findings we hand the model. High-materiality first; the
# tail is rarely informative once we're past the top few hundred and the
# extra tokens cost real money.
MAX_FINDINGS = 400
# Per-finding text budget (chars). Title + summary together; the model
# only needs enough context to spot a company name + what they did.
FINDING_SNIPPET_CHARS = 400

CATEGORIES = ["job_board", "ats", "labour_hire", "adjacent", "other"]

_client = anthropic.Anthropic()


def _render_skill(
    our_company: str,
    our_industry: str,
    existing: list[str],
    dismissed: list[str],
    hint: str | None,
) -> str:
    """Fill the discover_competitors skill template. Falls back to a
    minimal built-in prompt if the skill file is missing, so discovery
    still works on a fresh install before the seed pass runs."""
    template = load_active("discover_competitors") or _FALLBACK_PROMPT
    vals = {
        "our_company":    our_company or "our company",
        "our_industry":   our_industry or "our industry",
        "existing_list":  "\n".join(f"- {d}" for d in existing) or "- (none)",
        "dismissed_list": "\n".join(f"- {d}" for d in dismissed) or "- (none)",
        "hint":           (hint or "").strip(),
    }
    out = template
    for k, v in vals.items():
        out = out.replace("{{" + k + "}}", v)
    if vals["hint"]:
        out = out.replace("{{#hint}}", "").replace("{{/hint}}", "")
    else:
        out = _strip_block(out, "{{#hint}}", "{{/hint}}")
    return out


def _strip_block(text: str, open_tag: str, close_tag: str) -> str:
    while True:
        i = text.find(open_tag)
        if i < 0:
            return text
        j = text.find(close_tag, i)
        if j < 0:
            return text[:i]
        text = text[:i] + text[j + len(close_tag):]


_FALLBACK_PROMPT = """\
You are a competitive-intelligence analyst for {{our_company}} in {{our_industry}}.

You will be given a JSON array of recent findings. Each finding's
`competitor` is a company we ALREADY track — that's not who you're
looking for. Mine the `title` and `snippet` for OTHER company names
mentioned that {{our_company}} should consider tracking.

Already tracked (do not return):
{{existing_list}}

Previously dismissed (do not return):
{{dismissed_list}}

{{#hint}}Focus: {{hint}}{{/hint}}

For each candidate return: name, homepage_domain (apex if confidently
inferable, else null), category from [job_board, ats, labour_hire,
adjacent, other], a one-sentence one_line_why grounded in the findings,
and up to 5 finding_ids you used as evidence.

Respond with ONLY a JSON object:
{"candidates":[{"name":"...","homepage_domain":"...","category":"...","one_line_why":"...","finding_ids":[1,2]}]}
"""


def _normalize_domain(raw) -> str | None:
    """Strip scheme / path / www, lowercase, validate charset. Return
    None if unusable for dedup."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    import re
    if not re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", s):
        return None
    return s


def _build_findings_blob(findings: Iterable[Finding]) -> tuple[list[dict], dict[int, dict]]:
    """Project Findings to a slim payload for the LLM and an id→meta
    lookup the caller uses to build evidence links once candidates come
    back."""
    blob: list[dict] = []
    lookup: dict[int, dict] = {}
    for f in findings:
        text = (f.summary or f.content or "").strip()
        title = (f.title or "").strip()
        if not text and not title:
            continue
        snippet = text[:FINDING_SNIPPET_CHARS] if text else ""
        blob.append({
            "id": f.id,
            "competitor": f.competitor,
            "title": title[:200],
            "snippet": snippet,
        })
        lookup[f.id] = {
            "title": title or (f.url or f"finding #{f.id}"),
            "url": f.url or "",
        }
    return blob, lookup


def _normalize_candidates(
    payload: dict,
    exclude_domains: set[str],
    exclude_names_lc: set[str],
    finding_lookup: dict[int, dict],
) -> list[dict]:
    """Coerce the agent's candidate list into clean dicts. Drops entries
    missing a name, entries already on the exclude lists, and entries
    past the cap. Dedupes within the same response by domain (when
    present) and by lower-cased name (always)."""
    raw = payload.get("candidates")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen_domains: set[str] = set()
    seen_names: set[str] = set()

    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        name_lc = name.lower()
        if name_lc in exclude_names_lc or name_lc in seen_names:
            continue

        domain = _normalize_domain(item.get("homepage_domain"))
        if domain:
            if domain in exclude_domains or domain in seen_domains:
                continue
            seen_domains.add(domain)

        category = str(item.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            category = None

        why = str(item.get("one_line_why") or "").strip()

        # Build evidence from cited finding ids. Cap at 5 and silently
        # drop unknown ids — the model occasionally hallucinates extras.
        evidence: list[dict] = []
        ids_raw = item.get("finding_ids")
        if isinstance(ids_raw, list):
            for fid in ids_raw[:5]:
                try:
                    fid_int = int(fid)
                except (TypeError, ValueError):
                    continue
                meta = finding_lookup.get(fid_int)
                if not meta or not meta.get("url"):
                    continue
                evidence.append({
                    "title": meta["title"],
                    "url": meta["url"],
                })

        seen_names.add(name_lc)
        out.append({
            "name": name,
            "homepage_domain": domain,
            "category": category,
            "one_line_why": why,
            "evidence": evidence,
        })
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def discover_stream(
    db: Session,
    our_company: str,
    our_industry: str,
    existing_names: list[str],
    existing_domains: list[str],
    dismissed_names: list[str],
    dismissed_domains: list[str],
    hint: str | None = None,
) -> Iterator[dict]:
    """Generator — yields progress events ending with either
    {"type": "done", "candidates": [...]} or {"type": "error", "message": ...}.

    Never raises; fatal errors are emitted as error events so callers can
    drive an SSE stream or collect the list via the blocking wrapper
    without handling exceptions.
    """
    exclude_domains = {d for d in (existing_domains + dismissed_domains) if d}
    exclude_names_lc = {n.lower() for n in (existing_names + dismissed_names) if n}

    if hint:
        yield {"type": "progress", "message": f"discovering with focus: {hint[:80]}"}
    else:
        yield {"type": "progress", "message": "discovering new competitors"}

    cutoff = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
    yield {"type": "progress", "message": f"loading findings from last {LOOKBACK_DAYS} days"}

    findings = (
        db.query(Finding)
        .filter(Finding.created_at >= cutoff)
        # NULL materiality sorts last under "NULLS LAST" in PG; SQLite
        # treats NULL as smallest with DESC, which is what we want here
        # (rated low/zero rows fall to the bottom). created_at is the
        # tiebreaker so we prefer recent on equal-weight findings.
        .order_by(desc(Finding.materiality), desc(Finding.created_at))
        .limit(MAX_FINDINGS)
        .all()
    )

    if not findings:
        yield {"type": "progress", "message": "no findings in window — nothing to mine"}
        yield {"type": "done", "candidates": []}
        return

    blob, lookup = _build_findings_blob(findings)
    if not blob:
        yield {"type": "progress", "message": "findings have no usable text — nothing to mine"}
        yield {"type": "done", "candidates": []}
        return

    yield {
        "type": "progress",
        "message": f"scanning {len(blob)} findings for new competitor mentions",
    }

    system = _render_skill(
        our_company, our_industry,
        existing=existing_names + existing_domains,
        dismissed=dismissed_names + dismissed_domains,
        hint=hint,
    )
    user_msg = (
        "Recent findings to mine (JSON):\n"
        + json.dumps({"findings": blob}, ensure_ascii=False)
    )

    yield {"type": "progress", "message": "asking the model to surface candidates"}

    try:
        resp = _client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
        return

    final_text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    )
    payload = _extract_json(final_text) or {}
    candidates = _normalize_candidates(
        payload, exclude_domains, exclude_names_lc, lookup,
    )
    yield {"type": "done", "candidates": candidates}


def discover(
    db: Session,
    our_company: str,
    our_industry: str,
    existing_names: list[str],
    existing_domains: list[str],
    dismissed_names: list[str],
    dismissed_domains: list[str],
    hint: str | None = None,
) -> list[dict]:
    """Blocking variant — drains the stream and returns the candidate list."""
    for event in discover_stream(
        db, our_company, our_industry,
        existing_names, existing_domains,
        dismissed_names, dismissed_domains,
        hint=hint,
    ):
        if event["type"] == "error":
            raise RuntimeError(event["message"])
        if event["type"] == "done":
            return event.get("candidates", [])
    return []
