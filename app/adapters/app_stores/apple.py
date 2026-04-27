"""Apple App Store review adapter.

Apple publishes a public, key-free JSON feed of reviews per app/country/page:
    https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortby=mostrecent/page={page}/json

Up to 50 reviews per page, ~10 pages = ~500 reviews. No quota in practice;
we still sleep 500ms between pages out of politeness.

The first entry in each page's `feed.entry` is the *app metadata*, not a
review (Apple's RSS quirk). We skip it. After that, each entry has the
shape we map below.

Errors are not raised: callers handle ingest source-by-source, so a bad
country code or removed app should fail soft and let the rest of the
sweep continue. We return whatever we collected before the failure.
"""
from __future__ import annotations

import time
from datetime import datetime

import httpx

from .types import ReviewRow


_BASE = "https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortby=mostrecent/page={page}/json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_TIMEOUT_S = 20.0
_PAGE_SLEEP_S = 0.5


def _parse_entry(entry: dict) -> ReviewRow | None:
    """Map one Apple RSS JSON entry to a ReviewRow. Returns None if the
    entry is missing fields we treat as required (id, body)."""
    try:
        store_review_id = entry["id"]["label"]
        body = entry["content"]["label"]
    except (KeyError, TypeError):
        return None

    if not store_review_id or not body:
        return None

    title = (entry.get("title") or {}).get("label")
    rating_raw = (entry.get("im:rating") or {}).get("label")
    try:
        rating = int(rating_raw) if rating_raw is not None else None
    except (TypeError, ValueError):
        rating = None

    author_block = entry.get("author") or {}
    author_name = ((author_block.get("name") or {}).get("label")) or None

    posted_raw = (entry.get("updated") or {}).get("label")
    posted_at: datetime | None = None
    if posted_raw:
        # Apple sends ISO-8601 with offset, e.g. "2026-04-21T08:14:32-07:00".
        # fromisoformat handles this on Python 3.11+.
        try:
            posted_at = datetime.fromisoformat(posted_raw)
            # Strip tz to match the rest of the codebase, which stores
            # naive UTC datetimes (datetime.utcnow everywhere).
            if posted_at.tzinfo is not None:
                posted_at = posted_at.astimezone(tz=None).replace(tzinfo=None)
        except ValueError:
            posted_at = None

    return ReviewRow(
        store="apple",
        store_review_id=store_review_id,
        rating=rating,
        title=title,
        body=body,
        author=author_name,
        lang=None,  # Apple's feed doesn't expose review language.
        posted_at=posted_at,
    )


def fetch_apple(
    app_id: str,
    country: str = "us",
    max_pages: int = 10,
) -> list[ReviewRow]:
    """Fetch reviews for one (app_id, country) tuple from the Apple RSS.

    Returns reviews latest-first across pages. Stops on the first page
    that returns no reviews (Apple ends pagination by serving an entries
    list with only the metadata header)."""
    out: list[ReviewRow] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        url = _BASE.format(country=country.lower(), app_id=app_id, page=page)
        try:
            resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT_S)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            # Surface to caller as "no more reviews on this source this run".
            # Caller decides whether to record `last_error`.
            break

        feed = data.get("feed") or {}
        entries = feed.get("entry") or []
        # Apple's quirk: when there's only one "entry" it's a dict, not a
        # list. That single entry is also always the app-metadata header,
        # never a review, so we treat it as "page exhausted."
        if isinstance(entries, dict) or len(entries) <= 1:
            break

        # Skip the first entry (app metadata header).
        page_rows = 0
        for raw in entries[1:]:
            row = _parse_entry(raw)
            if row is None:
                continue
            if row.store_review_id in seen_ids:
                # Apple occasionally repeats a review across adjacent
                # pages when new reviews push the boundary; skip.
                continue
            seen_ids.add(row.store_review_id)
            out.append(row)
            page_rows += 1

        if page_rows == 0:
            break

        if page < max_pages:
            time.sleep(_PAGE_SLEEP_S)

    return out
