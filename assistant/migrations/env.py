"""Alembic environment. Runs synchronously against the configured database."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from assistant.core.config import ConfigService
from assistant.core.db import sync_url
from assistant.core.models import Base

target_metadata = Base.metadata


def _url() -> str:
    # Set by Database.migrate(); falls back to the app config for the alembic CLI.
    url = context.config.get_main_option("sqlalchemy.url")
    return url or sync_url(ConfigService.load().config.database_url)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
