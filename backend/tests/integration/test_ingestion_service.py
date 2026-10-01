import copy
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Earthquake, IngestionRun, IngestionStatus
from app.ingestion.bmkg_client import BmkgClient
from app.ingestion.domain import Feed
from app.ingestion.service import FeedIngestionResult, content_hash, ingest_all_feeds
from tests.bmkg_samples import BASE_URL, DISTINCT_QUAKES, load

SETTINGS = Settings(bmkg_base_url=BASE_URL, bmkg_max_attempts=2, bmkg_retry_backoff_seconds=0)

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
async def bmkg_client() -> AsyncIterator[BmkgClient]:
    async with httpx.AsyncClient() as http:
        yield BmkgClient.from_settings(http, SETTINGS)


def serve(respx_mock: respx.MockRouter, payloads: dict[Feed, Any]) -> None:
    """Make BMKG answer each feed with the given payload (an int means that HTTP status)."""
    for feed, payload in payloads.items():
        route = respx_mock.get(f"{BASE_URL}{feed.value}.json")
        if isinstance(payload, int):
            route.respond(payload)
        else:
            route.respond(json=payload)


def real_samples() -> dict[Feed, Any]:
    return {feed: load(feed) for feed in Feed}


def statuses(results: list[FeedIngestionResult]) -> dict[Feed, IngestionStatus]:
    return {result.feed: result.status for result in results}


async def quake_count(session_factory: Factory) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(Earthquake)) or 0


async def runs(session_factory: Factory) -> list[IngestionRun]:
    async with session_factory() as session:
        result = await session.scalars(
            select(IngestionRun).order_by(IngestionRun.fetched_at, IngestionRun.feed)
        )
        return list(result.all())


async def test_first_poll_stores_every_feed_and_records_a_run_per_feed(
    session_factory: Factory, bmkg_client: BmkgClient, respx_mock: respx.MockRouter
) -> None:
    serve(respx_mock, real_samples())

    results = await ingest_all_feeds(session_factory, bmkg_client, SETTINGS)

    assert statuses(results) == dict.fromkeys(Feed, IngestionStatus.SUCCESS)
    assert await quake_count(session_factory) == DISTINCT_QUAKES
    by_feed = {r.feed: r for r in results}
    assert by_feed[Feed.AUTOGEMPA].inserted_count == 1
    assert by_feed[Feed.GEMPATERKINI].inserted_count == 15
    # Its first item is the autogempa quake: merged (felt info), not inserted.
    assert by_feed[Feed.GEMPADIRASAKAN].inserted_count == 14
    assert by_feed[Feed.GEMPADIRASAKAN].updated_count == 1

    recorded = await runs(session_factory)
    assert [(r.feed, r.status) for r in recorded] == [
        (feed, IngestionStatus.SUCCESS) for feed in Feed
    ]
    assert all(r.content_hash == content_hash(load(r.feed)) for r in recorded)
    assert sum(r.inserted_count for r in recorded) == DISTINCT_QUAKES


async def test_unchanged_feeds_are_skipped_by_content_hash(
    session_factory: Factory, bmkg_client: BmkgClient, respx_mock: respx.MockRouter
) -> None:
    serve(respx_mock, real_samples())
    await ingest_all_feeds(session_factory, bmkg_client, SETTINGS)

    results = await ingest_all_feeds(session_factory, bmkg_client, SETTINGS)

    assert statuses(results) == dict.fromkeys(Feed, IngestionStatus.SKIPPED)
    assert all(r.inserted_count == r.updated_count == 0 for r in results)
    assert len(await runs(session_factory)) == 6
    assert await quake_count(session_factory) == DISTINCT_QUAKES


async def test_only_the_changed_feed_is_processed(
    session_factory: Factory, bmkg_client: BmkgClient, respx_mock: respx.MockRouter
) -> None:
    serve(respx_mock, real_samples())
    await ingest_all_feeds(session_factory, bmkg_client, SETTINGS)

    new_quake = copy.deepcopy(load(Feed.AUTOGEMPA))
    new_quake["Infogempa"]["gempa"].update(
        {"DateTime": "2026-10-01T08:00:00+00:00", "Coordinates": "-7.80,110.36"}
    )
    serve(respx_mock, {Feed.AUTOGEMPA: new_quake})
    results = await ingest_all_feeds(session_factory, bmkg_client, SETTINGS)

    assert statuses(results) == {
        Feed.AUTOGEMPA: IngestionStatus.SUCCESS,
        Feed.GEMPATERKINI: IngestionStatus.SKIPPED,
        Feed.GEMPADIRASAKAN: IngestionStatus.SKIPPED,
    }
    assert await quake_count(session_factory) == DISTINCT_QUAKES + 1


async def test_hash_ignores_json_formatting_and_key_order() -> None:
    payload = load(Feed.AUTOGEMPA)
    reordered = {"Infogempa": {"gempa": dict(reversed(payload["Infogempa"]["gempa"].items()))}}

    assert content_hash(reordered) == content_hash(payload)


async def test_bmkg_down_records_failed_runs_and_recovers_on_the_next_poll(
    session_factory: Factory, bmkg_client: BmkgClient, respx_mock: respx.MockRouter
) -> None:
    serve(respx_mock, dict.fromkeys(Feed, 503))

    down = await ingest_all_feeds(session_factory, bmkg_client, SETTINGS)

    assert statuses(down) == dict.fromkeys(Feed, IngestionStatus.FAILED)
    assert all(r.error is not None and r.error.startswith("BmkgUnavailableError") for r in down)
    failed_runs = await runs(session_factory)
    assert [r.status for r in failed_runs] == [IngestionStatus.FAILED] * 3
    assert all(r.content_hash is None and "HTTP 503" in (r.error or "") for r in failed_runs)
    assert await quake_count(session_factory) == 0

    serve(respx_mock, real_samples())
    back = await ingest_all_feeds(session_factory, bmkg_client, SETTINGS)

    # Failed runs never count as "last successful hash", so nothing is wrongly skipped.
    assert statuses(back) == dict.fromkeys(Feed, IngestionStatus.SUCCESS)
    assert await quake_count(session_factory) == DISTINCT_QUAKES


async def test_one_feed_failing_does_not_affect_the_others(
    session_factory: Factory, bmkg_client: BmkgClient, respx_mock: respx.MockRouter
) -> None:
    serve(respx_mock, real_samples())
    respx_mock.get(f"{BASE_URL}gempaterkini.json").mock(side_effect=httpx.ConnectError("refused"))

    results = await ingest_all_feeds(session_factory, bmkg_client, SETTINGS)

    assert statuses(results) == {
        Feed.AUTOGEMPA: IngestionStatus.SUCCESS,
        Feed.GEMPATERKINI: IngestionStatus.FAILED,
        Feed.GEMPADIRASAKAN: IngestionStatus.SUCCESS,
    }
    assert await quake_count(session_factory) == 15  # autogempa's quake is in dirasakan


async def test_unparseable_payload_is_a_failed_run_and_stores_nothing(
    session_factory: Factory, bmkg_client: BmkgClient, respx_mock: respx.MockRouter
) -> None:
    serve(respx_mock, {**real_samples(), Feed.GEMPATERKINI: {"unexpected": "shape"}})

    results = await ingest_all_feeds(session_factory, bmkg_client, SETTINGS)

    by_feed = {r.feed: r for r in results}
    assert by_feed[Feed.GEMPATERKINI].status is IngestionStatus.FAILED
    assert (by_feed[Feed.GEMPATERKINI].error or "").startswith("BmkgParseError")
    terkini_run = next(r for r in await runs(session_factory) if r.feed is Feed.GEMPATERKINI)
    assert terkini_run.content_hash == content_hash({"unexpected": "shape"})
