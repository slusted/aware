from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..deps import get_current_user, require_role
from .. import env_keys

router = APIRouter(prefix="/api/settings/keys", tags=["settings"])


class KeyIn(BaseModel):
    value: str


@router.get("")
def list_keys(_=Depends(get_current_user)):
    """Status only — never returns raw values, only a masked hint."""
    return env_keys.status()


@router.put("/{name}")
def set_key(name: str, payload: KeyIn, _=Depends(require_role("admin"))):
    try:
        env_keys.set_key(name, payload.value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "name": name, "set": True}


@router.delete("/{name}", status_code=204)
def clear_key(name: str, _=Depends(require_role("admin"))):
    try:
        env_keys.clear_key(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
