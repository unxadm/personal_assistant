"""Uploaded media: type detection by content, and image storage on disk.

File types are decided from the bytes, never from the name or Content-Type the client
sent, so only real images are stored and served back.
"""

from __future__ import annotations

import base64
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..core.config import AppConfig
from ..core.db import Database
from ..core.models import Attachment

log = logging.getLogger(__name__)

ORPHAN_AFTER = timedelta(hours=24)


class MediaError(ValueError):
    """The upload is not acceptable (wrong type, too large)."""


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """``(mime, extension)`` for JPEG, PNG, GIF and WebP; None for anything else."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def sniff_audio(data: bytes) -> tuple[str, str] | None:
    """``(mime, extension)`` for webm, ogg, m4a/mp4, mp3, wav and flac."""
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm", "webm"
    if data[:4] == b"OggS":
        return "audio/ogg", "ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if data[:4] == b"fLaC":
        return "audio/flac", "flac"
    if data[4:8] == b"ftyp":
        return "audio/mp4", "m4a"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg", "mp3"
    return None


class AttachmentStore:
    """One user's uploads. The files share one directory under unguessable names; the
    database row says whose they are, and every lookup checks it."""

    def __init__(self, db: Database, get_config: Callable[[], AppConfig], user_id: int):
        self._db = db
        self._get_config = get_config
        self._user_id = user_id

    @property
    def _dir(self) -> Path:
        path = self._get_config().data_path / "attachments"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def view(row: Attachment) -> dict[str, Any]:
        return {"id": row.id, "mime": row.mime, "filename": row.filename, "size": row.size}

    async def save_image(self, data: bytes, filename: str = "") -> dict[str, Any]:
        limit = self._get_config().uploads.max_image_mb
        if len(data) > limit * 1024 * 1024:
            raise MediaError(f"image is larger than {limit:g} MB")
        kind = sniff_image(data)
        if kind is None:
            raise MediaError("only JPEG, PNG, GIF and WebP images are supported")
        mime, ext = kind
        stored = f"{secrets.token_hex(16)}.{ext}"
        (self._dir / stored).write_bytes(data)
        row = Attachment(
            user_id=self._user_id, mime=mime, stored_name=stored, size=len(data),
            filename=Path(filename).name[:200] or f"image.{ext}",
        )
        async with self._db.session() as s:
            s.add(row)
            await s.commit()
        return self.view(row)

    async def get(self, attachment_id: int) -> tuple[Attachment, Path] | None:
        async with self._db.session() as s:
            row = await s.get(Attachment, attachment_id)
        if row is None or row.user_id != self._user_id:
            return None
        path = self._dir / row.stored_name
        return (row, path) if path.is_file() else None

    async def claim(self, ids: list[int], message_id: int) -> list[dict[str, Any]]:
        """Attach uploaded images to a message. Unknown or already-used ids are refused."""
        if len(ids) > self._get_config().uploads.max_images_per_message:
            raise MediaError(
                f"at most {self._get_config().uploads.max_images_per_message} images per message"
            )
        async with self._db.session() as s:
            rows = (
                await s.scalars(
                    select(Attachment).where(
                        Attachment.id.in_(ids), Attachment.user_id == self._user_id
                    )
                )
            ).all()
            by_id = {r.id: r for r in rows}
            if len(by_id) != len(set(ids)) or any(r.message_id is not None for r in rows):
                raise MediaError("attachment not found; upload the image again")
            for row in rows:
                row.message_id = message_id
            await s.commit()
        return [self.view(by_id[i]) for i in ids]

    async def for_messages(self, message_ids: list[int]) -> dict[int, list[Attachment]]:
        if not message_ids:
            return {}
        async with self._db.session() as s:
            rows = (
                await s.scalars(
                    select(Attachment)
                    .where(Attachment.user_id == self._user_id, Attachment.message_id.in_(message_ids))
                    .order_by(Attachment.id)
                )
            ).all()
        out: dict[int, list[Attachment]] = {}
        for row in rows:
            out.setdefault(row.message_id, []).append(row)
        return out

    def data_url(self, row: Attachment) -> str | None:
        path = self._dir / row.stored_name
        if not path.is_file():
            return None
        return f"data:{row.mime};base64,{base64.b64encode(path.read_bytes()).decode()}"

    async def set_description(self, attachment_id: int, description: str) -> None:
        async with self._db.session() as s:
            row = await s.get(Attachment, attachment_id)
            if row is not None and row.user_id == self._user_id:
                row.description = description
                await s.commit()

    async def _remove(self, rows: list[Attachment]) -> int:
        if not rows:
            return 0
        async with self._db.session() as s:
            for row in rows:
                (self._dir / row.stored_name).unlink(missing_ok=True)
                await s.delete(await s.merge(row))
            await s.commit()
        return len(rows)

    async def delete_for_messages(self, message_ids: list[int]) -> int:
        grouped = await self.for_messages(message_ids)
        return await self._remove([row for rows in grouped.values() for row in rows])

    async def cleanup(self, now: datetime) -> int:
        """Scheduler job: drop uploads that were never sent with a message."""
        async with self._db.session() as s:
            rows = (
                await s.scalars(
                    select(Attachment).where(
                        Attachment.user_id == self._user_id,
                        Attachment.message_id.is_(None), Attachment.created_at < now - ORPHAN_AFTER,
                    )
                )
            ).all()
        return await self._remove(list(rows))
