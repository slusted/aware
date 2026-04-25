"""Finding-embedding helper for spec 08.

One function — `embed_finding_text` — that takes the same dict shape the
classifier sees and returns `(blob, model)` ready to attach to a Finding
row. Both values are None on any failure path; callers persist the
finding either way (degraded scoring, not a dropped row).

The shared text recipe lives here so every save site (run_competitor_review,
run_scan_job customer pass, run_customer_scan_job) uses the same thing.
A model bump only needs to update app/ranker/config.py.
"""
from __future__ import annotations

from ..adapters import voyage as _voyage
from ..ranker import config as rcfg


def embed_finding_text(
    title: str | None,
    summary: str | None,
    content: str | None,
) -> tuple[bytes | None, str | None]:
    """Build the canonical embedding input for a finding and call Voyage.

    Recipe: `title + "\\n\\n" + (summary or content)`. The summary, when
    present, is the LLM's display-ready prose — denser signal than raw
    scraped content. Falls back to content for legacy / regex-classified
    rows where the LLM call didn't produce a summary.

    Returns `(None, None)` on any failure (missing key, API error, empty
    text, dim mismatch). Never raises.
    """
    title_part = (title or "").strip()
    body_part = (summary or content or "").strip()
    if not title_part and not body_part:
        return None, None
    text = (title_part + "\n\n" + body_part).strip()
    vec = _voyage.embed_document(text)
    if vec is None:
        return None, None
    return _voyage.pack(vec), rcfg.EMBEDDING_MODEL
