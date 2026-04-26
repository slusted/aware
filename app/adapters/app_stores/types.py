"""Normalised review row returned by every store adapter.

Keeping this in its own module avoids a circular import between the
package `__init__` and the per-store adapters."""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ReviewRow:
    store: str                       # "apple" | "play"
    store_review_id: str              # the store's stable id
    rating: int | None                # 1–5
    title: str | None
    body: str
    author: str | None
    lang: str | None
    posted_at: datetime | None
