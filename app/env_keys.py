"""Write API keys from the UI straight into .env + live process, without
requiring a restart. Security posture:
  - only a whitelist of known keys can be set (no arbitrary env writes)
  - keys are never returned in GET responses, only a masked preview
  - .env is expected to be git-ignored (the project already does this)
  - admin-role required at the route layer
"""
import os
from pathlib import Path

# Keys the UI is allowed to manage. Anything else is rejected at the route.
#
# `category` controls where each key surfaces in the UI:
#   - "engine"        → /settings/keys (model + search infrastructure)
#   - "notifications" → /admin/notifications (mail transport + reply poll)
#
# Storage path is identical for both — same .env file, same in-process
# refresh — only the rendering split differs (docs/chat/02-scheduled-
# questions.md §"Settings & admin placement").
MANAGED_KEYS: dict[str, dict] = {
    "ANTHROPIC_API_KEY": {
        "category": "engine",
        "description": "Claude (analysis, per-competitor reviews, briefs)",
        "required": True,
    },
    "GEMINI_API_KEY": {
        "category": "engine",
        "description": "Gemini Deep Research (per-competitor on-demand deep dives)",
    },
    "BRAVE_API_KEY": {
        "category": "engine",
        "description": "Brave Search (primary news provider — independent index, leads fan-out)",
    },
    "TAVILY_API_KEY": {
        "category": "engine",
        "description": "Tavily (page-content extraction; secondary in news fan-out)",
        "required": True,
    },
    "SERPER_API_KEY": {
        "category": "engine",
        "description": "Serper (Google News, optional)",
    },
    "ZENROWS_API_KEY": {
        "category": "engine",
        "description": "ZenRows (primary paid scraper — premium proxy + JS render, used first for every URL)",
    },
    "SCRAPINGBEE_API_KEY": {
        "category": "engine",
        "description": "ScrapingBee (secondary paid proxy fallback for Cloudflare-protected pages)",
    },
    "VOYAGE_API_KEY": {
        "category": "engine",
        "description": "Voyage AI (embeddings for semantic ranking — spec 08)",
    },
    "GMAIL_USER": {
        "category": "notifications",
        "description": "Gmail address used to send digests, scheduled-question emails, and to receive replies via IMAP.",
    },
    "GMAIL_APP_PASSWORD": {
        "category": "notifications",
        "description": "Gmail App Password (16-char). Requires 2FA on the Gmail account; generate at myaccount.google.com/apppasswords.",
    },
    "CHAT_REPLY_POLL_MINUTES": {
        "category": "notifications",
        "description": "How often to poll Gmail for replies to scheduled-question emails. Default 5 minutes. Restart the app for a change to take effect.",
    },
}

ENV_PATH = Path(os.environ.get("ENV_PATH", ".env"))


def _read_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def _write_lines(lines: list[str]):
    ENV_PATH.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _set_or_append(name: str, value: str):
    """Write KEY=VALUE to .env — replace the existing line if present,
    otherwise append. Naive parser: handles KEY=VALUE only (no shell quoting,
    no multi-line values). Comments preserved."""
    lines = _read_lines()
    prefix = f"{name}="
    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(prefix) or stripped.startswith(f"# {name}="):
            lines[i] = f"{name}={value}"
            found = True
            break
    if not found:
        lines.append(f"{name}={value}")
    _write_lines(lines)


def _remove_line(name: str):
    lines = _read_lines()
    keep = [l for l in lines if not l.lstrip().startswith(f"{name}=")]
    _write_lines(keep)


def set_key(name: str, value: str) -> None:
    """Persist to .env + os.environ + refresh any in-memory captures so the
    change takes effect immediately."""
    if name not in MANAGED_KEYS:
        raise ValueError(f"not a managed key: {name}")
    value = (value or "").strip()
    if not value:
        raise ValueError("value must not be empty — use delete to unset")
    _set_or_append(name, value)
    os.environ[name] = value
    _refresh_module_captures(name, value)


def clear_key(name: str) -> None:
    if name not in MANAGED_KEYS:
        raise ValueError(f"not a managed key: {name}")
    _remove_line(name)
    os.environ.pop(name, None)
    _refresh_module_captures(name, "")


def _refresh_module_captures(name: str, value: str) -> None:
    """Several engine modules read keys at import time into module-level
    constants. After changing a key, push the new value into those captures
    so the next call picks it up without a server restart."""
    try:
        if name == "TAVILY_API_KEY":
            from app.search_providers import tavily as _tavily
            _tavily.TAVILY_API_KEY = value
        elif name == "GMAIL_USER":
            import mailer
            mailer.GMAIL_USER = value
        elif name == "GMAIL_APP_PASSWORD":
            import mailer
            mailer.GMAIL_PASSWORD = value
        elif name == "ANTHROPIC_API_KEY":
            # Re-create every cached Anthropic client so the new key takes
            # effect without a restart. Three flavors to handle:
            #  1) Module-level instances (engine modules) — replace in place
            #     and re-wrap for usage tracking.
            #  2) Module-level instance in app.competitor_autofill / deepen —
            #     same shape, but these aren't wrapped for usage tracking
            #     today; just recreating them fixes the stale-key bug.
            #  3) Lazy-init caches (app.signals.summarize / llm_classify) —
            #     null the cache so the next call re-reads os.environ.
            import anthropic, analyzer, competitor_manager, deepen
            from . import competitor_autofill as _autofill
            from .signals import summarize as _sig_sum, llm_classify as _sig_llm
            from .usage import _wrap_anthropic_client
            analyzer.client = anthropic.Anthropic()
            competitor_manager.client = anthropic.Anthropic()
            _wrap_anthropic_client(analyzer.client)
            _wrap_anthropic_client(competitor_manager.client)
            _autofill._client = anthropic.Anthropic()
            deepen._client = anthropic.Anthropic()
            _sig_sum._client = None
            _sig_llm._client = None
        elif name == "GEMINI_API_KEY":
            # Null the adapter's cached client so the next call re-reads the env.
            from .adapters import gemini_research as _gem
            _gem._client = None
        elif name == "VOYAGE_API_KEY":
            # Same lazy-init pattern — null the cache so the next embed
            # call instantiates a fresh voyageai.Client with the new key.
            from .adapters import voyage as _voyage
            _voyage._client = None

        # If the key belongs to a search provider, rebuild the active-providers
        # set from config.json. Without this, a provider added via /settings/keys
        # would stay out of `_active` (which is frozen at startup or at PUT
        # /api/providers/{name}) until a restart, even though the key is live.
        from . import search_providers as _sp
        provider_env_vars = {cls.env_var for cls in _sp.REGISTRY.values()}
        if name in provider_env_vars:
            import json as _json
            cfg_path = os.environ.get("CONFIG_PATH", "config.json")
            with open(cfg_path, encoding="utf-8") as f:
                _cfg = _json.load(f)
            _sp.load_from_config(_cfg)
    except Exception as e:
        print(f"[env_keys] refresh failed for {name}: {e}")


def status(category: str | None = None) -> list[dict]:
    """Shape for /settings/keys and /admin/notifications — one row per
    managed key with a masked hint. Filter by ``category`` to scope to
    one tab; pass None for everything (used by the JSON list endpoint
    so external callers don't have to know about the split)."""
    out = []
    for name, meta in MANAGED_KEYS.items():
        if category is not None and meta.get("category") != category:
            continue
        val = os.environ.get(name, "")
        out.append({
            "name": name,
            "description": meta.get("description", ""),
            "category": meta.get("category", "engine"),
            "set": bool(val),
            "hint": _mask(val),
            "required": bool(meta.get("required", False)),
        })
    return out


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * len(value)
    return value[:4] + "…" + value[-3:]
