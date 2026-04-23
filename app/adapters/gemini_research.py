"""Thin wrapper over the Gemini Interactions API for Deep Research.

The Interactions API is the only surface that exposes Deep Research (as of
Dec 2025 GA). A Deep Research task must run with background=True because a
single run can take 5–20 minutes; we create the interaction, get an id
back, and poll until terminal.

Public contract:
  - start_research(brief, agent) -> interaction_id (str)
  - poll_research(interaction_id) -> dict with keys:
      status      : "running" | "ready" | "failed"
      body_md     : str (empty until ready)
      sources     : list[dict]    (empty until ready)
      error       : str | None
      model       : str | None    (the actual model string Gemini used)
      cost_usd    : float | None  (best-effort, None when usage metadata absent)

Everything Gemini-specific lives in here. The job wrapper treats this as an
opaque state machine; tests stub `_client` with an object exposing the same
method shape.
"""
from __future__ import annotations

import os
import threading
from typing import Any

# Lazy client cache — rebuilt when env_keys.py notifies a key rotation.
_client: Any | None = None
_client_lock = threading.Lock()


# Model/agent names — pinned to the April 2026 previews. Update here when
# Gemini promotes newer snapshots; nothing else needs to change.
AGENT_PREVIEW = "deep-research-preview-04-2026"
AGENT_MAX = "deep-research-max-preview-04-2026"


class GeminiUnavailable(RuntimeError):
    """Raised when the SDK isn't installed or GEMINI_API_KEY is missing.
    The job wrapper catches this and marks the report failed with a
    human-readable error the tab can show."""


def _resolve_agent(agent: str) -> str:
    a = (agent or "").lower().strip()
    if a in ("max", "deep-research-max", AGENT_MAX):
        return AGENT_MAX
    # Default to preview for any unknown string — safer (cheaper, faster).
    return AGENT_PREVIEW


def _get_client():
    """Return a cached google-genai client, constructing it lazily.
    Raises GeminiUnavailable when prerequisites aren't met."""
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise GeminiUnavailable(
                "GEMINI_API_KEY is not set. Add it on /settings/keys."
            )
        try:
            from google import genai  # type: ignore
        except ImportError as e:
            raise GeminiUnavailable(
                "google-genai is not installed. `pip install google-genai>=1.20`."
            ) from e
        _client = genai.Client(api_key=key)
        return _client


def start_research(brief: str, agent: str = "preview") -> str:
    """Kick off a Deep Research interaction and return the interaction id.
    Runs in background mode — this call returns in seconds; the actual
    research takes minutes and is picked up by poll_research()."""
    client = _get_client()
    agent_id = _resolve_agent(agent)
    try:
        # Interactions API: create returns an Interaction with an .id that
        # stays valid for polling. background=True is load-bearing for
        # Deep Research — without it, the SDK blocks the whole call.
        interaction = client.interactions.create(
            input=brief,
            agent=agent_id,
            background=True,
        )
    except Exception as e:
        raise GeminiUnavailable(
            f"Gemini interactions.create failed: {type(e).__name__}: {e}"
        ) from e
    iid = getattr(interaction, "id", None) or getattr(interaction, "name", None)
    if not iid:
        raise GeminiUnavailable(
            "Gemini returned no interaction id — cannot resume polling."
        )
    return str(iid)


def poll_research(interaction_id: str) -> dict:
    """One polling tick. Returns a normalized dict; never raises on
    transient errors (callers just retry). GeminiUnavailable is still
    raised for configuration problems — those don't get better on retry."""
    client = _get_client()
    try:
        interaction = client.interactions.get(interaction_id)
    except Exception as e:
        return {
            "status": "running",
            "body_md": "",
            "sources": [],
            "error": None,
            "model": None,
            "cost_usd": None,
            "_transient_error": f"{type(e).__name__}: {e}",
        }
    return _normalize(interaction)


def _normalize(interaction: Any) -> dict:
    """Map Gemini's Interaction shape into the small dict the job wrapper
    consumes. Tolerant of SDK-shape drift — any missing field falls back
    to a safe default."""
    raw_status = str(getattr(interaction, "status", "") or "").lower()

    # Gemini statuses we've seen: "queued", "running", "in_progress",
    # "completed", "succeeded", "failed", "error", "cancelled".
    if raw_status in ("completed", "succeeded", "success", "done", "ready"):
        status = "ready"
    elif raw_status in ("failed", "error", "cancelled", "canceled"):
        status = "failed"
    else:
        status = "running"

    body_md = ""
    sources: list[dict] = []
    model = getattr(interaction, "model", None) or getattr(interaction, "agent", None)
    cost_usd: float | None = None
    err: str | None = None

    if status == "failed":
        err = (
            getattr(interaction, "error_message", None)
            or str(getattr(interaction, "error", "") or "")
            or "Gemini reported failure with no details."
        )

    if status == "ready":
        body_md = _extract_text(interaction)
        sources = _extract_sources(interaction)
        cost_usd = _extract_cost(interaction)
        # Gemini says the interaction completed but we couldn't pull any
        # body or citations out of the response. This is almost always a
        # shape-drift issue (the SDK moved the fields). Flip to failed
        # with a diagnostic dump so the UI shows something actionable
        # instead of a blank "Report body is empty." panel.
        if not body_md and not sources:
            status = "failed"
            err = (
                "Gemini returned a completed interaction but no body or "
                "sources were extracted. The SDK response shape likely "
                "drifted. Diagnostic dump (first few fields):\n"
                + _debug_dump(interaction)
            )

    return {
        "status": status,
        "body_md": body_md,
        "sources": sources,
        "error": err,
        "model": str(model) if model else None,
        "cost_usd": cost_usd,
    }


def _extract_text(interaction: Any) -> str:
    """Pull markdown out of an Interaction object. Walks every plausible
    location the SDK might put the body — if any path yields non-empty
    text, return it. Returns '' when nothing matches (caller promotes
    that to a diagnostic failure)."""
    # 1. The originally-documented Interactions API shape: .output / .result
    #    carry a text object or list of parts.
    for attr in ("output", "result", "response"):
        t = _text_from(getattr(interaction, attr, None))
        if t:
            return t
    # 2. generate_content-style: candidates[*].content.parts[*].text —
    #    either directly on the interaction or nested under .response.
    for holder in (getattr(interaction, "response", None), interaction):
        cands = getattr(holder, "candidates", None) if holder is not None else None
        if not cands:
            continue
        for cand in cands:
            t = _text_from(getattr(cand, "content", None))
            if t:
                return t
    # 3. Shortcut attrs some SDK variants expose at the top level.
    for attr in ("text", "body", "markdown", "content"):
        v = getattr(interaction, attr, None)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _text_from(obj: Any) -> str:
    """Lower-level: extract text from a str / object-with-.text /
    list-of-parts / dict. Shared by the candidate paths in
    `_extract_text`."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    t = getattr(obj, "text", None)
    if isinstance(t, str) and t.strip():
        return t
    parts = getattr(obj, "parts", None) or (obj if isinstance(obj, list) else None)
    if parts:
        chunks: list[str] = []
        for p in parts:
            pt = getattr(p, "text", None)
            if isinstance(pt, str):
                chunks.append(pt)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                chunks.append(p["text"])
        if chunks:
            joined = "\n\n".join(chunks).strip()
            if joined:
                return joined
    if isinstance(obj, dict):
        for k in ("text", "markdown", "body_md", "content"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def _extract_sources(interaction: Any) -> list[dict]:
    """Normalize citations/grounding metadata into our schema.

    Shape we return:
      [{"title": str, "url": str,
        "published_at": str | None, "snippet": str | None}]
    """
    raw: list = []
    # 1. Direct citation-ish fields on the interaction.
    for attr in ("citations", "sources", "grounding"):
        val = getattr(interaction, attr, None)
        if val:
            raw = list(val) if isinstance(val, (list, tuple)) else [val]
            break
    # 2. generate_content-style grounding chunks under candidates.
    if not raw:
        for holder in (getattr(interaction, "response", None), interaction):
            cands = getattr(holder, "candidates", None) if holder is not None else None
            if not cands:
                continue
            for cand in cands:
                gm = getattr(cand, "grounding_metadata", None)
                if gm is None and isinstance(cand, dict):
                    gm = cand.get("grounding_metadata")
                chunks = getattr(gm, "grounding_chunks", None) if gm is not None else None
                if chunks is None and isinstance(gm, dict):
                    chunks = gm.get("grounding_chunks")
                if chunks:
                    raw.extend(chunks)
    out: list[dict] = []
    for item in raw or []:
        # Grounding chunks wrap their payload under .web
        web = getattr(item, "web", None) if not isinstance(item, dict) else item.get("web")
        if web is not None:
            item = web
        if isinstance(item, dict):
            url = item.get("url") or item.get("uri") or item.get("source") or ""
            title = item.get("title") or item.get("name") or url
            published = item.get("published_at") or item.get("published") or item.get("date")
            snippet = item.get("snippet") or item.get("summary")
        else:
            url = getattr(item, "url", None) or getattr(item, "uri", None) or ""
            title = getattr(item, "title", None) or getattr(item, "name", None) or url
            published = getattr(item, "published_at", None) or getattr(item, "published", None)
            snippet = getattr(item, "snippet", None) or getattr(item, "summary", None)
        if not url:
            continue
        out.append({
            "title": str(title or url)[:300],
            "url": str(url),
            "published_at": str(published) if published else None,
            "snippet": str(snippet)[:400] if snippet else None,
        })
    return out


def _debug_dump(obj: Any, depth: int = 0, max_depth: int = 3) -> str:
    """Best-effort introspection of whatever Gemini returned, formatted
    so the error field in the Research tab is readable. Only runs when
    extraction came up empty — the cost of the reflection doesn't matter
    at that point, and seeing the shape is what unblocks the fix."""
    if depth > max_depth:
        return "…"
    if obj is None:
        return "None"
    if isinstance(obj, (str, int, float, bool)):
        s = repr(obj)
        return s[:200] + ("…" if len(s) > 200 else "")
    if isinstance(obj, (list, tuple)):
        if not obj:
            return "[]"
        inner = ", ".join(_debug_dump(x, depth + 1, max_depth) for x in list(obj)[:3])
        more = f", … ({len(obj)} total)" if len(obj) > 3 else ""
        return f"[{inner}{more}]"
    if isinstance(obj, dict):
        items = list(obj.items())[:10]
        inner = ", ".join(
            f"{k!r}: {_debug_dump(v, depth + 1, max_depth)}" for k, v in items
        )
        return "{" + inner + "}"
    attrs = [a for a in dir(obj) if not a.startswith("_")]
    lines = [f"<{type(obj).__name__}>"]
    for a in attrs[:15]:
        try:
            v = getattr(obj, a)
        except Exception:
            continue
        if callable(v):
            continue
        lines.append(f"  .{a} = {_debug_dump(v, depth + 1, max_depth)}")
    return "\n".join(lines)


def _extract_cost(interaction: Any) -> float | None:
    """Best-effort: pull a USD cost from usage metadata if the API
    exposes it. Returns None when unavailable — the UI handles that."""
    usage = getattr(interaction, "usage_metadata", None) or getattr(interaction, "usage", None)
    if usage is None:
        return None
    for key in ("total_cost_usd", "cost_usd", "total_cost"):
        val = None
        if isinstance(usage, dict):
            val = usage.get(key)
        else:
            val = getattr(usage, key, None)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None
