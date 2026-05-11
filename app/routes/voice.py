"""Voice input — speech-to-text via Groq Whisper.

Browser records audio with MediaRecorder, POSTs the blob here as multipart
form data, we forward it to Groq's OpenAI-compatible transcription endpoint,
and return ``{"text": "..."}``. The transcript fills the chat textarea so
the user can review before sending.

Auth: every endpoint requires a logged-in user (same as the rest of the app).
GROQ_API_KEY is managed through /settings/keys like the other engine keys.
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from ..deps import get_current_user
from ..models import User


router = APIRouter(prefix="/api/voice", tags=["voice"])

# Groq's OpenAI-compatible audio endpoint. Same shape as OpenAI's,
# so a future swap is one URL + key change.
_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_MODEL = "whisper-large-v3-turbo"
_MAX_BYTES = 25 * 1024 * 1024  # Groq's documented limit
_TIMEOUT_S = 60.0


@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    _: User = Depends(get_current_user),
):
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            400,
            "GROQ_API_KEY is not set. Add it on /settings/keys to enable voice input.",
        )

    blob = await audio.read()
    if not blob:
        raise HTTPException(400, "empty audio")
    if len(blob) > _MAX_BYTES:
        raise HTTPException(413, f"audio exceeds {_MAX_BYTES // (1024 * 1024)}MB limit")

    filename = audio.filename or "audio.webm"
    content_type = audio.content_type or "audio/webm"

    files = {"file": (filename, blob, content_type)}
    data = {"model": _MODEL, "response_format": "json"}
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(_GROQ_URL, headers=headers, files=files, data=data)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"groq transport error: {e}")

    if resp.status_code != 200:
        raise HTTPException(502, f"groq {resp.status_code}: {resp.text[:300]}")

    payload = resp.json()
    text = (payload.get("text") or "").strip()
    return {"text": text}
