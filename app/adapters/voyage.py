"""Voyage AI embeddings adapter (docs/ranker/08-semantic-ranking.md).

One thin seam between the rest of the app and `voyageai.Client`. Two
public functions — `embed_document` and `embed_documents` — both return
`None` per item on any error rather than raising. Callers (extractor,
backfill, rollup) all treat "no embedding" as "this card scores without
the embedding term", so an outage degrades silently.

Module-level lazy client matches the pattern used by
`app/signals/llm_classify.py`. `app/env_keys._refresh_module_captures`
nulls `_client` on key change so a key flip via /settings/keys takes
effect without a restart.

Cost telemetry is recorded via `UsageEvent` rows (provider="voyage").
Rate-limit / 5xx errors are logged once per (failure-mode, hour) so a
sustained outage doesn't spam the run log.
"""
from __future__ import annotations

import os
import time
import traceback
from typing import Iterable

import numpy as np

from ..db import SessionLocal
from ..models import UsageEvent
from ..pricing import voyage_cost
from ..ranker import config as rcfg
from ..usage import current_run_id


# Lazy import — keeps `pip install -e .` working in environments where
# voyageai isn't installed yet (e.g. test harness for code paths that
# never call the adapter). The actual import happens inside _get_client.
_client = None


def _get_client():
    """Return a memoized voyageai.Client, or None if the key is unset
    or the SDK isn't importable."""
    global _client
    if _client is not None:
        return _client
    if not os.environ.get("VOYAGE_API_KEY"):
        return None
    try:
        import voyageai  # type: ignore[import-not-found]
    except ImportError:
        _log_once("import_error", "voyageai package not installed")
        return None
    _client = voyageai.Client()  # picks up VOYAGE_API_KEY from env
    return _client


# ── Error log throttling ────────────────────────────────────────────

_last_log_at: dict[str, float] = {}
_LOG_INTERVAL_SEC: float = 3600.0  # one log per failure-mode per hour


def _log_once(mode: str, msg: str) -> None:
    now = time.time()
    last = _last_log_at.get(mode)
    if last is None or now - last >= _LOG_INTERVAL_SEC:
        _last_log_at[mode] = now
        print(f"[voyage:{mode}] {msg}")


# ── Cost recording ──────────────────────────────────────────────────

def _record_usage(model: str, total_tokens: int, success: bool, batch_size: int) -> None:
    """Single UsageEvent row per call (single or batch). `total_tokens` from
    the Voyage response when available, else 0 (the success=False path)."""
    try:
        cost = voyage_cost(model, total_tokens) if success else 0.0
        db = SessionLocal()
        try:
            db.add(UsageEvent(
                run_id=current_run_id.get(),
                provider="voyage",
                operation="embed",
                model=model,
                input_tokens=total_tokens,
                output_tokens=0,
                cost_usd=cost,
                success=success,
                extra={"batch_size": batch_size},
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        traceback.print_exc()


# ── Public API ──────────────────────────────────────────────────────

def embed_document(text: str) -> np.ndarray | None:
    """Embed one piece of document-side text. Returns a float32 ndarray
    of length EMBEDDING_DIM, or None on any error.

    The vector is L2-normalized at the source by Voyage for the v3
    family — we still normalize defensively so cosine-as-dot-product
    holds regardless of model swaps.
    """
    if not text or not text.strip():
        return None
    out = embed_documents([text])
    return out[0] if out else None


def embed_documents(texts: list[str] | Iterable[str]) -> list[np.ndarray | None]:
    """Embed a batch of document-side texts. Returns a list aligned with
    the input — same length, same order. Slots that errored individually
    or that were empty come back as None.

    On a whole-batch failure (network, auth, rate limit) every slot
    returns None and a single UsageEvent row is recorded with
    success=False so the outage is visible on the admin page.
    """
    texts = list(texts)
    if not texts:
        return []

    # Normalize empties to None up front; only send non-empty rows to the API.
    cleaned: list[str | None] = []
    for t in texts:
        if not t or not t.strip():
            cleaned.append(None)
        else:
            cleaned.append(t.strip()[: rcfg.EMBEDDING_INPUT_CHAR_CAP])

    sendable_idx = [i for i, t in enumerate(cleaned) if t is not None]
    sendable = [cleaned[i] for i in sendable_idx]
    if not sendable:
        return [None] * len(texts)

    client = _get_client()
    if client is None:
        return [None] * len(texts)

    try:
        resp = client.embed(
            sendable,
            model=rcfg.EMBEDDING_MODEL,
            input_type="document",
        )
    except Exception as e:
        _log_once("api_error", f"{type(e).__name__}: {e}")
        _record_usage(rcfg.EMBEDDING_MODEL, 0, success=False, batch_size=len(sendable))
        return [None] * len(texts)

    # voyageai SDK returns an object with .embeddings (list of lists) and
    # .total_tokens. Defensive accessors so a minor SDK shape change doesn't
    # crash the ingest path.
    raw = getattr(resp, "embeddings", None)
    tokens = getattr(resp, "total_tokens", 0) or 0
    if not raw or len(raw) != len(sendable):
        _log_once("shape_error", f"unexpected response: {type(resp).__name__}")
        _record_usage(rcfg.EMBEDDING_MODEL, tokens, success=False, batch_size=len(sendable))
        return [None] * len(texts)

    _record_usage(rcfg.EMBEDDING_MODEL, tokens, success=True, batch_size=len(sendable))

    out: list[np.ndarray | None] = [None] * len(texts)
    for slot_i, vec in zip(sendable_idx, raw):
        try:
            arr = np.asarray(vec, dtype=np.float32)
        except (TypeError, ValueError):
            continue
        if arr.shape != (rcfg.EMBEDDING_DIM,):
            # Wrong-dim vector means the model in config.py disagrees with the
            # one Voyage actually served. Skip rather than persist a row that
            # would silently mismatch the centroid.
            _log_once("dim_mismatch",
                      f"got dim={arr.shape} want={rcfg.EMBEDDING_DIM}")
            continue
        norm = float(np.linalg.norm(arr))
        if norm > 0:
            arr = arr / norm
        out[slot_i] = arr.astype(np.float32, copy=False)
    return out


# ── Pack / unpack helpers ───────────────────────────────────────────
# Used by the rollup, scorer, and backfill so the on-disk format is
# defined in exactly one place.

def pack(vec: np.ndarray) -> bytes:
    """float32 ndarray → bytes for BLOB storage."""
    return np.ascontiguousarray(vec, dtype=np.float32).tobytes()


def unpack(blob: bytes | None) -> np.ndarray | None:
    """BLOB → float32 ndarray, or None if the blob is missing or has the
    wrong byte length (defensive — a model swap can leave stale rows)."""
    if blob is None:
        return None
    expected_bytes = rcfg.EMBEDDING_DIM * 4
    if len(blob) != expected_bytes:
        return None
    return np.frombuffer(blob, dtype=np.float32)
