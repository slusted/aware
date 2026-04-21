"""Server-side cache of competitor logos.

We fetch each competitor's logo once (from Apistemic) and store it under
``DATA_DIR/logos/{domain}.webp``. The stream template serves the local copy
via ``/logos/{domain}.webp``, so:
  - the rendered page makes no third-party requests (privacy + latency),
  - logos keep working if Apistemic goes down,
  - swapping to a different provider is a one-line change in this module.

No DB bookkeeping — presence of the file IS the cache hit. Missing files
just mean no logo is rendered; the caller never errors.
"""
from __future__ import annotations

import os
import re
import urllib.request
from pathlib import Path
from sqlalchemy.orm import Session


LOGOS_DIR = Path(os.environ.get("DATA_DIR", "data")) / "logos"

# Apistemic domain lookup. Monogram fallback means they hand back a letter
# badge for unknown domains instead of 404 — we still cache that; switching
# providers later is a one-line swap.
_PROVIDER_URL = "https://logos-api.apistemic.com/domain:{domain}?fallback=monogram"

# Conservative domain validator — lowercase letters, digits, dots, hyphens.
# Blocks path separators and anything else that could escape LOGOS_DIR.
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,127}$")

_FETCH_TIMEOUT = 10.0


def _sanitize(domain: str | None) -> str | None:
    if not domain:
        return None
    d = domain.strip().lower()
    return d if _DOMAIN_RE.match(d) else None


def logo_path(domain: str | None) -> Path | None:
    s = _sanitize(domain)
    return (LOGOS_DIR / f"{s}.webp") if s else None


def has_logo(domain: str | None) -> bool:
    p = logo_path(domain)
    return p is not None and p.is_file()


def logo_url(domain: str | None) -> str | None:
    """Public URL for the cached logo, or None when we don't have one."""
    s = _sanitize(domain)
    if not s or not has_logo(s):
        return None
    return f"/logos/{s}.webp"


def fetch_and_store(domain: str | None) -> bool:
    """Download the logo once and write it under LOGOS_DIR. Idempotent —
    safe to call repeatedly. Returns True on success."""
    s = _sanitize(domain)
    if not s:
        return False
    path = LOGOS_DIR / f"{s}.webp"
    try:
        LOGOS_DIR.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            _PROVIDER_URL.format(domain=s),
            headers={"User-Agent": "competitor-watch/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                return False
            data = resp.read()
        if not data:
            return False
        # Write atomically so a partial download can't masquerade as a hit.
        tmp = path.with_suffix(".webp.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return True
    except Exception as e:
        print(f"  [logos] fetch failed for {s}: {type(e).__name__}: {e}", flush=True)
        return False


def refetch_missing(db: Session) -> tuple[int, int]:
    """Fetch logos for every active competitor whose homepage_domain is set
    but whose on-disk copy is missing. Safe to run on every boot — skips
    anything already cached. Returns (attempted, succeeded)."""
    from .models import Competitor

    attempted = succeeded = 0
    rows = (
        db.query(Competitor)
        .filter(Competitor.active == True, Competitor.homepage_domain.isnot(None))
        .all()
    )
    for c in rows:
        if has_logo(c.homepage_domain):
            continue
        attempted += 1
        if fetch_and_store(c.homepage_domain):
            succeeded += 1
    return attempted, succeeded
