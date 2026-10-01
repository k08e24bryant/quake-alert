from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import IngestionRun, IngestionStatus
from app.ingestion.bmkg_client import BmkgClient
from app.ingestion.domain import Feed
from tests.bmkg_samples import BASE_URL, load
from worker.jobs import poll_bmkg_feeds, prune_old_ingestion_runs

SETTINGS = Settings(bmkg_base_url=BASE_URL, bmkg_max_attempts=1, bmkg_retry_backoff_seconds=0)


@pytest.fixture
async def ctx(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict[str, Any]]:
    """The arq ctx startup() would build, but bound to the rolled-back test transaction."""
    async with httpx.AsyncClient() as http:
        yield {
            "settings": SETTINGS,
            "session_factory": session_factory,
            "bmkg_client": BmkgClient.from_settings(http, SETTINGS),
        }


async def test_poll_job_ingests_every_feed_and_records_runs(
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    respx_mock: respx.MockRouter,
) -> None:
    for feed in Feed:
        respx_mock.get(f"{BASE_URL}{feed.value}.json").respond(json=load(feed))

    first = await poll_bmkg_feeds(ctx)
    second = await poll_bmkg_feeds(ctx)

    assert first == dict.fromkeys(Feed, "success")
    assert second == dict.fromkeys(Feed, "skipped")
    async with session_factory() as session:
        recorded = (await session.scalars(select(IngestionRun.status))).all()
    assert sorted(recorded) == sorted([IngestionStatus.SUCCESS] * 3 + [IngestionStatus.SKIPPED] * 3)


async def test_prune_job_deletes_expired_runs_but_keeps_the_latest_success(
    ctx: dict[str, Any], session_factory: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        session.add_all(
            IngestionRun(
                feed=feed, status=IngestionStatus.SUCCESS, fetched_at=now - timedelta(days=days)
            )
            for feed in Feed
            for days in (30, 60)
        )

    assert await prune_old_ingestion_runs(ctx) == 3

    async with session_factory() as session:
        kept = (await session.scalars(select(IngestionRun.fetched_at))).all()
    assert len(kept) == 3
    assert all(now - fetched_at < timedelta(days=31) for fetched_at in kept)
