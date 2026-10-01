import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db import models  # noqa: F401  (registers models on Base.metadata)
from app.db.base import Base

config = context.config

# Callers that manage logging themselves (e.g. the test suite) set
# attributes["configure_logger"] = False.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    # An explicit sqlalchemy.url (e.g. set by the test suite) wins over app settings.
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _configure_kwargs() -> dict[str, Any]:
    # GeoAlchemy2 helpers keep PostGIS-managed objects (spatial_ref_sys, implicit spatial
    # indexes) out of autogenerate and render geometry/geography columns correctly.
    return {
        "target_metadata": target_metadata,
        "include_object": alembic_helpers.include_object,
        "process_revision_directives": alembic_helpers.writer,
        "render_item": alembic_helpers.render_item,
        "compare_type": True,
    }


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_configure_kwargs())
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
