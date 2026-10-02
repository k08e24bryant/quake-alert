import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from arq import ArqRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis
from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.ingestion.bmkg_client import BmkgClient
from app.ingestion.freshness import StalenessMonitor
from app.main import create_app
from app.notifications.dispatcher import NotifyConfig
from app.notifications.telegram import TelegramClient
from tests import telegram_samples
from tests.bmkg_samples import BASE_URL

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


async def run_alembic(database_url: URL, action: str, *args: str) -> None:
    # env.py calls asyncio.run(), which cannot nest inside the test event loop.
    await asyncio.to_thread(getattr(command, action), alembic_config(database_url), *args)


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
async def db_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection inside a transaction that is rolled back after the test."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()


@pytest.fixture
def session_factory(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Sessions bound to the test transaction, for code that opens its own sessions.

    Code under test may begin/commit freely; with create_savepoint that only creates and
    releases SAVEPOINTs, so nothing leaks between tests.
    """
    return async_sessionmaker(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def redis_client(integration_settings: IntegrationSettings) -> AsyncIterator[Redis]:
    redis = Redis.from_url(integration_settings.test_redis_url, decode_responses=True)
    await redis.flushdb()
    yield redis
    await redis.flushdb()
    await redis.aclose()


@pytest.fixture
async def arq_redis(
    integration_settings: IntegrationSettings, redis_client: Redis
) -> AsyncIterator[ArqRedis]:
    """arq's client on the test Redis db (flushed by redis_client), for code that enqueues
    jobs. Bytes in and out, unlike redis_client."""
    redis = ArqRedis.from_url(integration_settings.test_redis_url)
    yield redis
    await redis.aclose()


@pytest.fixture
def app_settings(database_url: URL, integration_settings: IntegrationSettings) -> Settings:
    return Settings(
        environment="test",
        database_url=database_url.render_as_string(hide_password=False),
        redis_url=integration_settings.test_redis_url,
        telegram_webhook_secret=SecretStr(telegram_samples.WEBHOOK_SECRET),
        # Explicit, so values in a developer's backend/.env can't change test behaviour.
        webhook_secret_keys=SecretStr(""),
        cors_allowed_origins="",
    )


@pytest.fixture
async def client(app_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(app_settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
async def api_app(
    app_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[FastAPI]:
    """The app with its DB sessions bound to the rolled-back test transaction, so rows a
    test seeds are visible to the API and vanish afterwards. Redis is the (flushed) test db."""
    app = create_app(app_settings)
    async with app.router.lifespan_context(app):
        app.state.sessionmaker = session_factory
        yield app


@pytest.fixture
async def api(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as c:
        yield c


# What the worker runs with in tests: BMKG and Telegram are mocked with respx, and retries
# don't sleep.
WORKER_SETTINGS = Settings(
    bmkg_base_url=BASE_URL,
    bmkg_max_attempts=1,
    bmkg_retry_backoff_seconds=0,
    telegram_bot_token=SecretStr(telegram_samples.TOKEN),
    telegram_api_base_url=telegram_samples.API_BASE,
    notify_retry_backoff_seconds=2,
)


@pytest.fixture
async def worker_ctx(
    session_factory: async_sessionmaker[AsyncSession], arq_redis: ArqRedis
) -> AsyncIterator[dict[str, Any]]:
    """The arq ctx startup() builds, bound to the rolled-back test transaction. `redis` stands
    in for arq's own connection; `job_try` is what arq sets for each run of a job."""
    async with httpx.AsyncClient() as http:
        yield {
            "settings": WORKER_SETTINGS,
            "session_factory": session_factory,
            "bmkg_client": BmkgClient.from_settings(http, WORKER_SETTINGS),
            "telegram": TelegramClient.from_settings(http, WORKER_SETTINGS),
            "notify_config": NotifyConfig.from_settings(WORKER_SETTINGS),
            "staleness_monitor": StalenessMonitor(),
            "redis": arq_redis,
            "job_try": 1,
        }
