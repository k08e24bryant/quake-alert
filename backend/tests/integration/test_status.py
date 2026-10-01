import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IngestionRun, IngestionStatus
from app.ingestion.domain import Feed
from tests.bmkg_samples import BASE_URL, load
from tests.integration.seed import seed_quake
from worker.jobs import poll_bmkg_feeds


async def add_runs(session: AsyncSession, *runs: tuple[Feed, IngestionStatus, timedelta]) -> None:
    """Runs as (feed, status, how long ago)."""
    now = datetime.now(UTC)
    session.add_all(
        IngestionRun(feed=feed, status=status, fetched_at=now - ago) for feed, status, ago in runs
    )
    await session.flush()


def by_feed(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {feed["feed"]: feed for feed in body["feeds"]}


def parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp)


async def test_fresh_when_every_feed_succeeded_recently(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await add_runs(
        db_session,
        (Feed.AUTOGEMPA, IngestionStatus.SUCCESS, timedelta(minutes=1)),
        # skipped = fetched fine, content unchanged: still a successful read.
        (Feed.GEMPATERKINI, IngestionStatus.SKIPPED, timedelta(minutes=2)),
        (Feed.GEMPADIRASAKAN, IngestionStatus.SUCCESS, timedelta(minutes=3)),
    )

    response = await api.get("/v1/status")

    assert response.status_code == 200
    body = response.json()
    assert body["ingestion_state"] == "ok"
    assert body["stale_after_minutes"] == 5
    assert set(by_feed(body)) == {"autogempa", "gempaterkini", "gempadirasakan"}
    assert by_feed(body)["gempaterkini"]["last_run_status"] == "skipped"
    assert body["checked_at"].endswith("+00:00")
    assert body["source"]["name"].startswith("BMKG")
    autogempa_success = parse(by_feed(body)["autogempa"]["last_success_at"])
    assert parse(body["source"]["data_as_of"]) == autogempa_success  # the latest success


async def test_one_feed_stale_when_its_recent_runs_failed(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await add_runs(
        db_session,
        (Feed.AUTOGEMPA, IngestionStatus.SUCCESS, timedelta(minutes=1)),
        (Feed.GEMPATERKINI, IngestionStatus.SUCCESS, timedelta(minutes=20)),
        (Feed.GEMPATERKINI, IngestionStatus.FAILED, timedelta(minutes=1)),
        (Feed.GEMPADIRASAKAN, IngestionStatus.SUCCESS, timedelta(minutes=1)),
    )

    body = (await api.get("/v1/status")).json()

    assert body["ingestion_state"] == "stale"
    terkini = by_feed(body)["gempaterkini"]
    assert terkini["last_run_status"] == "failed"
    assert parse(terkini["last_run_at"]) > parse(terkini["last_success_at"])
    assert datetime.now(UTC) - parse(terkini["last_success_at"]) > timedelta(minutes=19)


async def test_never_run_feed_is_stale_with_nulls(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await add_runs(
        db_session,
        (Feed.AUTOGEMPA, IngestionStatus.SUCCESS, timedelta(minutes=1)),
        (Feed.GEMPATERKINI, IngestionStatus.SUCCESS, timedelta(minutes=1)),
    )

    body = (await api.get("/v1/status")).json()

    assert body["ingestion_state"] == "stale"
    assert by_feed(body)["gempadirasakan"] == {
        "feed": "gempadirasakan",
        "last_success_at": None,
        "last_run_status": None,
        "last_run_at": None,
    }


async def test_recovers_once_a_new_successful_run_lands(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await add_runs(
        db_session, *((feed, IngestionStatus.SUCCESS, timedelta(hours=1)) for feed in Feed)
    )
    assert (await api.get("/v1/status")).json()["ingestion_state"] == "stale"

    await add_runs(db_session, *((feed, IngestionStatus.SKIPPED, timedelta(0)) for feed in Feed))

    assert (await api.get("/v1/status")).json()["ingestion_state"] == "ok"


async def test_status_works_without_redis(
    api: AsyncClient, db_session: AsyncSession, api_app: Any
) -> None:
    await add_runs(db_session, *((feed, IngestionStatus.SUCCESS, timedelta(0)) for feed in Feed))
    await api_app.state.redis.aclose()  # every Redis call now fails; rate limiting fails open

    response = await api.get("/v1/status")

    assert response.status_code == 200
    assert response.json()["ingestion_state"] == "ok"


async def test_earthquake_responses_carry_data_as_of(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    row = await seed_quake(db_session)
    await add_runs(
        db_session,
        (Feed.AUTOGEMPA, IngestionStatus.SUCCESS, timedelta(minutes=3)),
        (Feed.GEMPATERKINI, IngestionStatus.SKIPPED, timedelta(minutes=1)),
        (Feed.GEMPADIRASAKAN, IngestionStatus.FAILED, timedelta(seconds=10)),  # doesn't count
    )
    status = (await api.get("/v1/status")).json()
    expected = by_feed(status)["gempaterkini"]["last_success_at"]

    for path in ("/v1/earthquakes", "/v1/earthquakes/latest", f"/v1/earthquakes/{row.id}"):
        body = (await api.get(path)).json()
        assert body["source"]["data_as_of"] == expected, path


async def test_poll_job_logs_staleness_transitions_once(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.ingestion.freshness")
    for feed in Feed:
        respx_mock.get(f"{BASE_URL}{feed.value}.json").respond(503)

    await poll_bmkg_feeds(worker_ctx)  # every feed fails: never succeeded -> stale
    await poll_bmkg_feeds(worker_ctx)  # still stale: no second warning
    respx_mock.reset()
    for feed in Feed:
        respx_mock.get(f"{BASE_URL}{feed.value}.json").respond(json=load(feed))
    await poll_bmkg_feeds(worker_ctx)  # recovered
    await poll_bmkg_feeds(worker_ctx)

    messages = [r.getMessage() for r in caplog.records if r.name == "app.ingestion.freshness"]
    assert messages == ["BMKG ingestion is stale", "BMKG ingestion recovered"]
