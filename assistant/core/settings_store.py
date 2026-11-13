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
    def __init__(self, db: Database, box: SecretBox):
        self._db = db
        self._box = box

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._db.session() as s:
            row = await s.get(Setting, key)
        if row is None:
            return default
        return self._box.decrypt(row.value) if row.encrypted else row.value

    async def set(self, key: str, value: Any, *, secret: bool = False) -> None:
        stored = self._box.encrypt(value) if secret else value
        async with self._db.session() as s:
            row = await s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=stored, encrypted=secret))
            else:
                row.value, row.encrypted, row.updated_at = stored, secret, utcnow()
            await s.commit()

    async def delete(self, key: str) -> None:
        async with self._db.session() as s:
            await s.execute(delete(Setting).where(Setting.key == key))
            await s.commit()

    async def load_overrides(self) -> dict[str, Any]:
        """Config overrides as {dotted.path: value}."""
        async with self._db.session() as s:
            rows = (
                await s.scalars(select(Setting).where(Setting.key.startswith(CONFIG_PREFIX)))
            ).all()
        return {
            r.key[len(CONFIG_PREFIX) :]: self._box.decrypt(r.value) if r.encrypted else r.value
            for r in rows
        }

    async def save_overrides(self, overrides: dict[str, Any]) -> None:
        """Replace all stored config overrides."""
        async with self._db.session() as s:
            await s.execute(delete(Setting).where(Setting.key.startswith(CONFIG_PREFIX)))
            for path, value in overrides.items():
                secret = is_secret_path(path) and isinstance(value, str)
                s.add(
                    Setting(
                        key=CONFIG_PREFIX + path,
                        value=self._box.encrypt(value) if secret else value,
                        encrypted=secret,
                    )
                )
            await s.commit()
