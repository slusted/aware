"""App-store review adapters.

Each adapter returns a list of `ReviewRow` (a normalised, store-agnostic
record). The orchestrator in `app/app_reviews.py` writes these to the
`app_reviews` table without caring which store they came from.

v1: Apple only. Spec 02 will add Google Play behind the same dataclass."""
from .types import ReviewRow
from .apple import fetch_apple

__all__ = ["ReviewRow", "fetch_apple"]
