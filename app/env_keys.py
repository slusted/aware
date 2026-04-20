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
MANAGED_KEYS: dict[str, str] = {
    "ANTHROPIC_API_KEY":   "Claude (analysis, per-competitor reviews, briefs)",
    "TAVILY_API_KEY":      "Tavily (primary web search)",
    "SERPER_API_KEY":      "Serper (Google News, optional)",
    "ZENROWS_API_KEY":     "ZenRows (primary paid scraper — premium proxy + JS render, used first for every URL)",
    "SCRAPINGBEE_API_KEY": "ScrapingBee (secondary paid proxy fallback for Cloudflare-protected pages)",
    "GMAIL_USER":          "Gmail account for digest emails",
    "GMAIL_APP_PASSWORD":  "Gmail app password (IMAP/SMTP)",
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
            # Re-create the shared Anthropic clients so the new key is used,
            # then re-install the usage-tracking wrapper on each.
            import anthropic, analyzer, competitor_manager
            from .usage import _wrap_anthropic_client
            analyzer.client = anthropic.Anthropic()
            competitor_manager.client = anthropic.Anthropic()
            _wrap_anthropic_client(analyzer.client)
            _wrap_anthropic_client(competitor_manager.client)
        # SERPER_API_KEY is read at call time by SerperProvider — nothing cached.
    except Exception as e:
        print(f"[env_keys] refresh failed for {name}: {e}")


def status() -> list[dict]:
    """Shape for /settings/keys — one row per managed key with a masked hint."""
    out = []
    for name, desc in MANAGED_KEYS.items():
        val = os.environ.get(name, "")
        out.append({
            "name": name,
            "description": desc,
            "set": bool(val),
            "hint": _mask(val),
            "required": name in ("ANTHROPIC_API_KEY", "TAVILY_API_KEY"),
        })
    return out


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * len(value)
    return value[:4] + "…" + value[-3:]
