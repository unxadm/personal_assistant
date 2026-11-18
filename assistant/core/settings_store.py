"""Persistence for settings-UI overrides and other small key/value state."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

from .config import is_secret_path
from .db import Database
from .models import Setting, utcnow
from .security import SecretBox

CONFIG_PREFIX = "config:"


class SettingsStore:
    """Key/value settings. The plain store holds server-wide values; ``scoped(user_id)`` is
    a view of one user's own (their keys carry a ``user:<id>:`` prefix, so nothing a user
    saves can collide with, or be read as, another user's or the server's)."""

    def __init__(self, db: Database, box: SecretBox, prefix: str = ""):
        self._db = db
        self._box = box
        self._prefix = prefix

    def scoped(self, user_id: int) -> SettingsStore:
        return SettingsStore(self._db, self._box, f"user:{user_id}:")

    async def delete_all(self) -> None:
        """Forget everything in this scope (used when a user is deleted)."""
        if not self._prefix:
            raise ValueError("refusing to clear the server-wide settings")
        async with self._db.session() as s:
            await s.execute(delete(Setting).where(Setting.key.startswith(self._prefix)))
            await s.commit()

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._db.session() as s:
            row = await s.get(Setting, self._prefix + key)
        if row is None:
            return default
        return self._box.decrypt(row.value) if row.encrypted else row.value

    async def set(self, key: str, value: Any, *, secret: bool = False) -> None:
        stored = self._box.encrypt(value) if secret else value
        async with self._db.session() as s:
            row = await s.get(Setting, self._prefix + key)
            if row is None:
                s.add(Setting(key=self._prefix + key, value=stored, encrypted=secret))
            else:
                row.value, row.encrypted, row.updated_at = stored, secret, utcnow()
            await s.commit()

    async def delete(self, key: str) -> None:
        async with self._db.session() as s:
            await s.execute(delete(Setting).where(Setting.key == self._prefix + key))
            await s.commit()

    async def load_overrides(self) -> dict[str, Any]:
        """Config overrides as {dotted.path: value}."""
        start = self._prefix + CONFIG_PREFIX
        async with self._db.session() as s:
            rows = (await s.scalars(select(Setting).where(Setting.key.startswith(start)))).all()
        return {
            r.key[len(start) :]: self._box.decrypt(r.value) if r.encrypted else r.value
            for r in rows
        }

    async def save_overrides(self, overrides: dict[str, Any]) -> None:
        """Replace all stored config overrides."""
        start = self._prefix + CONFIG_PREFIX
        async with self._db.session() as s:
            await s.execute(delete(Setting).where(Setting.key.startswith(start)))
            for path, value in overrides.items():
                secret = is_secret_path(path) and isinstance(value, str)
                s.add(
                    Setting(
                        key=start + path,
                        value=self._box.encrypt(value) if secret else value,
                        encrypted=secret,
                    )
                )
            await s.commit()
