"""Async database engine, sqlite-vec loading, and migrations."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class Database:
    def __init__(self, url: str):
        self.url = url
        self.is_sqlite = make_url(url).get_backend_name() == "sqlite"
        # True once sqlite-vec has loaded on a connection (enables SQL vector distance).
        self.vec_available = False
        self._vec_warned = False
        if self.is_sqlite:
            db_file = make_url(url).database
            if db_file and db_file != ":memory:":
                Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        if self.is_sqlite:
            event.listen(self.engine.sync_engine, "connect", self._on_sqlite_connect)

    def _on_sqlite_connect(self, dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
        try:
            import sqlite_vec

            dbapi_conn.run_async(lambda conn: conn.enable_load_extension(True))
            dbapi_conn.run_async(lambda conn: conn.load_extension(sqlite_vec.loadable_path()))
            dbapi_conn.run_async(lambda conn: conn.enable_load_extension(False))
            self.vec_available = True
        except Exception as e:
            self.vec_available = False
            if not self._vec_warned:
                self._vec_warned = True
                log.warning("sqlite-vec unavailable (%s); using in-process vector search", e)

    async def create_all(self) -> None:
        """Create tables directly from the models (tests / throwaway databases)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def migrate(self) -> None:
        """Run Alembic migrations up to head."""
        await asyncio.to_thread(run_migrations, self.url)

    async def dispose(self) -> None:
        await self.engine.dispose()


def sync_url(url: str) -> str:
    """Alembic runs synchronously; map async driver URLs to their sync equivalents."""
    return url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")


def run_migrations(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", sync_url(url).replace("%", "%%"))
    command.upgrade(cfg, "head")
