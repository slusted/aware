"""Read/write the `search_providers` block in config.json and reload the
active set. No restart required — `/api/providers` returns the current status;
PUT writes config.json and re-registers providers in-process."""
import json
import os
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_current_user, require_role
from .. import search_providers, fetcher as fetcher_module

router = APIRouter(prefix="/api/providers", tags=["providers"])


def _config_path() -> str:
    return os.environ.get("CONFIG_PATH", "config.json")


def _load_config() -> dict:
    with open(_config_path(), encoding="utf-8") as f:
        return json.load(f)


def _save_config(cfg: dict):
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


@router.get("")
def list_providers(_=Depends(get_current_user)):
    cfg = _load_config()
    return search_providers.provider_status(cfg)


class ProviderSettings(BaseModel):
    enabled: bool
    scope: list[str] = ["news"]


@router.put("/{name}")
def update_provider(name: str, payload: ProviderSettings, _=Depends(require_role("admin"))):
    if name not in search_providers.REGISTRY:
        raise HTTPException(404, f"unknown provider '{name}'")
    cfg = _load_config()
    block = cfg.setdefault("search_providers", {})
    block[name] = payload.model_dump()
    _save_config(cfg)
    search_providers.load_from_config(cfg)
    return {"ok": True, "name": name, **block[name]}


class FetcherSettings(BaseModel):
    scrapingbee_primary: bool | None = None
    zenrows_primary: bool | None = None


@router.get("/fetcher")
def get_fetcher(_=Depends(get_current_user)):
    cfg = _load_config()
    block = cfg.get("fetcher") or {}
    return {
        "zenrows_primary":    bool(block.get("zenrows_primary", True)),
        "zenrows_key_set":    bool(os.environ.get("ZENROWS_API_KEY", "")),
        "scrapingbee_primary": bool(block.get("scrapingbee_primary", False)),
        "scrapingbee_key_set": bool(os.environ.get("SCRAPINGBEE_API_KEY", "")),
    }


@router.put("/fetcher")
def put_fetcher(payload: FetcherSettings, _=Depends(require_role("admin"))):
    """Toggle fetcher behavior. Either toggle can be sent independently:
      - zenrows_primary=True sends every URL through ZenRows first (default).
      - scrapingbee_primary=True sends every URL through ScrapingBee first
        (only effective when zenrows_primary is off or ZenRows key is missing).
    """
    cfg = _load_config()
    block = cfg.setdefault("fetcher", {})
    if payload.zenrows_primary is not None:
        block["zenrows_primary"] = payload.zenrows_primary
    if payload.scrapingbee_primary is not None:
        block["scrapingbee_primary"] = payload.scrapingbee_primary
    _save_config(cfg)
    fetcher_module.configure(cfg)
    return {
        "ok": True,
        "zenrows_primary":    bool(block.get("zenrows_primary", True)),
        "scrapingbee_primary": bool(block.get("scrapingbee_primary", False)),
    }
