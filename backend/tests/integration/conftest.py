import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis
from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.config import Settings
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[2]


class IntegrationSettings(BaseSettings):
    """Where the integration suite finds its services. The test database is dropped and
    recreated on every run, so never point TEST_DATABASE_URL at a database you care about."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    test_database_url: str = "postgresql+asyncpg://quake:quake@localhost:5432/quake_alert_test"
    test_redis_url: str = "redis://localhost:6379/15"


def alembic_config(database_url: URL) -> Config:
    config = Config(BACKEND_DIR / "alembic.ini")
    # ConfigParser interpolation treats "%" specially; escape it in passwords.
    config.set_main_option(
        "sqlalchemy.url", database_url.render_as_string(hide_password=False).replace("%", "%%")
    )
    config.attributes["configure_logger"] = False
    return config


async def recreate_database(url: URL) -> None:
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = admin.dialect.identifier_preparer.quote(str(url.database))
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            await connection.execute(text(f"CREATE DATABASE {name}"))
    finally:
        await admin.dispose()


async def drop_database(url: URL) -> None:
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = admin.dialect.identifier_preparer.quote(str(url.database))
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    finally:
        await admin.dispose()


async def run_alembic(database_url: URL, action: str, revision: str) -> None:
    # env.py calls asyncio.run(), which cannot nest inside the test event loop.
    await asyncio.to_thread(getattr(command, action), alembic_config(database_url), revision)


@pytest.fixture(scope="session")
def integration_settings() -> IntegrationSettings:
    return IntegrationSettings()


@pytest.fixture(scope="session")
async def database_url(integration_settings: IntegrationSettings) -> AsyncIterator[URL]:
    """A fresh PostGIS test database, migrated to head through Alembic."""
    url = make_url(integration_settings.test_database_url)
    await recreate_database(url)
    await run_alembic(url, "upgrade", "head")
    yield url
    await drop_database(url)


@pytest.fixture(scope="session")
async def engine(database_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is rolled back after the test.

    Code under test may call commit(); with create_savepoint it only releases a SAVEPOINT,
    so nothing leaks between tests.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
async def redis_client(integration_settings: IntegrationSettings) -> AsyncIterator[Redis]:
    redis = Redis.from_url(integration_settings.test_redis_url, decode_responses=True)
    await redis.flushdb()
    yield redis
    await redis.flushdb()
    await redis.aclose()


@pytest.fixture
def app_settings(database_url: URL, integration_settings: IntegrationSettings) -> Settings:
    return Settings(
        environment="test",
        database_url=database_url.render_as_string(hide_password=False),
        redis_url=integration_settings.test_redis_url,
    )


@pytest.fixture
async def client(app_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(app_settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client
