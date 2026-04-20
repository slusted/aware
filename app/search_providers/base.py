"""Abstract base for search providers. Keep the interface narrow — everything
extends from `search_news` today; `search_general` can be added later if a
second provider wants to compete with Tavily on page extraction."""
from __future__ import annotations
from abc import ABC, abstractmethod


class SearchProvider(ABC):
    """Each provider declares:
      - name       — matches the key in config.search_providers{...}
      - description — shown on /settings/providers
      - env_var    — the API-key env var name for the settings page
    """

    name: str = ""
    description: str = ""
    env_var: str = ""

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Return True if this provider can run right now (API key present, etc)."""

    @abstractmethod
    def search_news(self, query: str, *, max_results: int = 5,
                    days: int | None = None) -> list[dict]:
        """Return a list of results in the scanner's shape:
          {title, content, snippet, url, score, source_provider, ...}
        """
