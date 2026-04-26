"""App-store review ingest pass.

Reads each enabled `AppReviewSource`, fetches its latest reviews via the
matching adapter, and upserts them into `app_reviews`. Dedup is by the
`(store, store_review_id)` unique constraint — duplicates are caught at
the database, not in Python.

No LLM calls here. The synthesis pass (`app/voc_themes.py`) is what reads
this corpus and produces themes. Decoupling the two means an LLM outage
never costs us reviews and an ingest hiccup never costs us themes.

Spec: docs/voc/01-app-reviews.md
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .adapters.app_stores import ReviewRow, fetch_apple
from .models import AppReview, AppReviewSource, Competitor


_DEFAULT_MAX_PAGES = 10


@dataclass
class IngestResult:
    sources_processed: int = 0
    sources_failed: int = 0
    rows_inserted: int = 0
    rows_skipped_dedup: int = 0


def _config_max_pages(config: dict | None) -> int:
    if not config:
        return _DEFAULT_MAX_PAGES
    block = config.get("app_reviews") or {}
    try:
        return int(block.get("ingest_max_pages", _DEFAULT_MAX_PAGES))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_PAGES


def _fetch_for_source(src: AppReviewSource, max_pages: int) -> list[ReviewRow]:
    if src.store == "apple":
        return fetch_apple(src.app_id, src.country, max_pages=max_pages)
    # Spec 02 will add: if src.store == "play": return fetch_play(...)
    return []


def _persist_rows(
    db: Session,
    src: AppReviewSource,
    rows: list[ReviewRow],
) -> tuple[int, int]:
    """Insert rows, skipping duplicates by the unique (store, store_review_id)
    constraint. Returns (inserted, skipped_dedup)."""
    inserted = 0
    skipped = 0
    for r in rows:
        review = AppReview(
            source_id=src.id,
            competitor_id=src.competitor_id,
            store=r.store,
            store_review_id=r.store_review_id,
            rating=r.rating,
            title=r.title,
            body=r.body,
            author=r.author,
            lang=r.lang,
            posted_at=r.posted_at,
        )
        db.add(review)
        try:
            db.commit()
            inserted += 1
        except IntegrityError:
            db.rollback()
            skipped += 1
    return inserted, skipped


def ingest_for_source(
    db: Session,
    src: AppReviewSource,
    max_pages: int = _DEFAULT_MAX_PAGES,
) -> tuple[int, int]:
    """Run one source. Returns (inserted, skipped_dedup). Errors set
    `last_error` on the source row but do not raise — caller wants to keep
    sweeping the rest of the install."""
    try:
        rows = _fetch_for_source(src, max_pages=max_pages)
    except Exception as e:  # pragma: no cover — adapter swallows most
        src.last_error = f"fetch failed: {e}"
        src.last_ingested_at = datetime.utcnow()
        src.last_ingested_count = 0
        db.commit()
        return (0, 0)

    inserted, skipped = _persist_rows(db, src, rows)
    src.last_error = None
    src.last_ingested_at = datetime.utcnow()
    src.last_ingested_count = inserted
    db.commit()
    return (inserted, skipped)


def ingest_for_competitor(
    db: Session,
    competitor: Competitor,
    config: dict | None = None,
) -> IngestResult:
    """Run every enabled source for one competitor. Used by manual
    triggers from the profile page."""
    max_pages = _config_max_pages(config)
    result = IngestResult()
    sources = (
        db.query(AppReviewSource)
        .filter(
            AppReviewSource.competitor_id == competitor.id,
            AppReviewSource.enabled == True,  # noqa: E712 — SQLAlchemy filter
        )
        .all()
    )
    for src in sources:
        result.sources_processed += 1
        try:
            ins, skp = ingest_for_source(db, src, max_pages=max_pages)
            result.rows_inserted += ins
            result.rows_skipped_dedup += skp
        except Exception:
            result.sources_failed += 1
    return result


def ingest_all(db: Session, config: dict | None = None) -> IngestResult:
    """Sweep every enabled source on the install. Cron entrypoint."""
    max_pages = _config_max_pages(config)
    result = IngestResult()
    sources = (
        db.query(AppReviewSource)
        .filter(AppReviewSource.enabled == True)  # noqa: E712
        .all()
    )
    for src in sources:
        result.sources_processed += 1
        try:
            ins, skp = ingest_for_source(db, src, max_pages=max_pages)
            result.rows_inserted += ins
            result.rows_skipped_dedup += skp
        except Exception:
            result.sources_failed += 1
    return result


def validate_apple_source(app_id: str, country: str = "us") -> tuple[bool, str | None]:
    """Used at admin-add time to surface bad ids before they go into the
    sources table. Hits the RSS endpoint with a small page budget; if we
    get any reviews it's good. Returns (ok, error_message)."""
    try:
        rows = fetch_apple(app_id, country, max_pages=1)
    except Exception as e:
        return (False, f"Apple RSS request failed: {e}")
    if not rows:
        return (
            False,
            f"No reviews found for app id {app_id!r} in country {country!r}. "
            "Check the id and country code.",
        )
    return (True, None)
