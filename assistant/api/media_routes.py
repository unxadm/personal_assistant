"""REST API: image uploads, voice transcription, spoken replies.

Uploads are sent as the raw request body (no multipart): the body is the file, the
``X-Filename`` header its optional name.
"""

from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..llm.client import LLMError, LLMNotConfigured
from ..media.store import MediaError, sniff_audio
from .auth import container, mine, require_auth

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


async def _read(request: Request, limit_mb: float, what: str) -> bytes:
    """Read the body, refusing anything over the limit without buffering all of it."""
    limit = int(limit_mb * 1024 * 1024)
    too_large = HTTPException(413, f"{what} is larger than {limit_mb:g} MB")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    if not size:
        raise HTTPException(422, "empty upload")
    return b"".join(chunks)


@router.post("/attachments", status_code=201)
async def upload_image(request: Request):
    c = container(request)
    data = await _read(request, c.config.config.uploads.max_image_mb, "image")
    try:
        return await mine(request).media.save_image(data, unquote(request.headers.get("x-filename", "")))
    except MediaError as e:
        raise HTTPException(415, str(e)) from None


@router.get("/attachments/{attachment_id}")
async def get_image(attachment_id: int, request: Request):
    found = await mine(request).media.get(attachment_id)
    if found is None:
        raise HTTPException(404, "Attachment not found")
    row, path = found
    return FileResponse(
        path, media_type=row.mime,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"},
    )


@router.post("/voice/transcribe")
async def transcribe(request: Request):
    """Speech to text. The audio is passed to the STT model and not stored."""
    c = container(request)
    data = await _read(request, c.config.config.uploads.max_audio_mb, "recording")
    kind = sniff_audio(data)
    if kind is None:
        raise HTTPException(415, "unsupported audio format (use webm, ogg, m4a, mp3, wav or flac)")
    mime, ext = kind
    try:
        return {"text": await c.llm.transcribe(data, f"audio.{ext}", mime)}
    except LLMNotConfigured:
        raise HTTPException(400, "No speech-to-text model is set. Add one under Settings.") from None
    except LLMError as e:
        raise HTTPException(502, f"Transcription failed: {e}") from None


class SpeakBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/voice/speak")
async def speak(body: SpeakBody, request: Request):
    """Text to speech for the "Listen" button."""
    try:
        audio, mime = await container(request).llm.speak(body.text)
    except LLMNotConfigured:
        raise HTTPException(400, "No text-to-speech model is set. Add one under Settings.") from None
    except LLMError as e:
        raise HTTPException(502, f"Speech failed: {e}") from None
    return Response(audio, media_type=mime, headers={"Cache-Control": "no-store"})
