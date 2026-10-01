from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Earthquake, NotificationDelivery
from app.ingestion.dedup import DedupConfig, UpsertOutcome, upsert_report
from app.ingestion.domain import Feed
from app.notifications.dispatcher import DeliveryOutcome, NotifyConfig, deliver
from app.notifications.telegram import TelegramClient
from scripts.dev_fake_quake import FakeQuake, GuardError, create_fake_quake
from tests.bmkg_samples import BASE_URL, DEFAULT_TIME, make_report
from tests.integration.seed import JAKARTA, seed_quake, seed_subscription
from tests.telegram_samples import API_BASE, TOKEN, error, method_url, ok, sent_messages


def settings(*, environment: str = "development", token: str = TOKEN) -> Settings:
    return Settings(
        environment=environment,  # type: ignore[arg-type]
        telegram_bot_token=SecretStr(token),
        telegram_api_base_url=API_BASE,
    )


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


async def fake(
    config: Settings,
    http: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> FakeQuake:
    lat, lon = JAKARTA
    return await create_fake_quake(
        config,
        TelegramClient.from_settings(http, config),
        session_factory,
        latitude=lat,
        longitude=lon,
        magnitude=Decimal("5.0"),
        depth_km=10,
        offset_km=10,
        now=datetime.now(UTC).replace(microsecond=0),
    )


async def quake_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Earthquake)) or 0


def get_me(respx_mock: respx.MockRouter, username: str | None) -> respx.Route:
    me: dict[str, Any] = {"id": 1, "is_bot": True, "first_name": "Quake"}
    if username is not None:
        me["username"] = username
    return respx_mock.post(method_url("getMe")).mock(return_value=ok(me))


# --- guards: each one refuses before anything is written -------------------------------------


@pytest.mark.parametrize("environment", ["production", "test"])
async def test_refuses_outside_development_without_calling_telegram(
    http: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
    environment: str,
) -> None:
    route = get_me(respx_mock, "quake_alert_dev_bot")

    with pytest.raises(GuardError, match="ENVIRONMENT"):
        await fake(settings(environment=environment), http, session_factory)

    assert not route.called
    assert await quake_count(db_session) == 0


async def test_refuses_without_a_bot_token(
    http: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    route = get_me(respx_mock, "quake_alert_dev_bot")

    with pytest.raises(GuardError, match="TELEGRAM_BOT_TOKEN"):
        await fake(settings(token=""), http, session_factory)

    assert not route.called
    assert await quake_count(db_session) == 0


async def test_refuses_when_get_me_fails(
    http: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(method_url("getMe")).mock(return_value=error(401, "Unauthorized"))

    with pytest.raises(GuardError, match="getMe") as caught:
        await fake(settings(), http, session_factory)

    assert TOKEN not in str(caught.value)
    assert await quake_count(db_session) == 0


@pytest.mark.parametrize(
    "username", ["quake_alert_bot", "dev_bot_quake_bot", "quake_dev_bot_2", "quakedevbot", None]
)
async def test_refuses_bots_whose_username_does_not_end_with_dev_bot(
    http: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
    username: str | None,
) -> None:
    get_me(respx_mock, username)

    with pytest.raises(GuardError, match="not a dev bot"):
        await fake(settings(), http, session_factory)

    assert await quake_count(db_session) == 0


# --- what it does once every guard passes ----------------------------------------------------


@pytest.mark.parametrize("username", ["quake_alert_dev_bot", "QuakeAlert_Dev_Bot"])
async def test_inserts_a_synthetic_quake_nearby_and_matches_it(
    http: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
    username: str,
) -> None:
    get_me(respx_mock, username)
    subscription = await seed_subscription(db_session, at=JAKARTA, radius_km=50)

    result = await fake(settings(), http, session_factory)

    assert result.bot_username == username
    assert len(result.delivery_ids) == 1
    row = await db_session.get(Earthquake, result.earthquake_id)
    assert row is not None
    assert row.is_synthetic is True
    assert row.needs_matching is False  # matched in the same transaction
    assert row.fingerprint.startswith("synthetic:")
    # 10 km north of the (rounded) point.
    assert (result.latitude, result.longitude) == (Decimal("-6.12"), Decimal("106.85"))
    delivery = await db_session.get(NotificationDelivery, result.delivery_ids[0])
    assert delivery is not None
    assert (delivery.subscription_id, delivery.earthquake_id) == (subscription.id, row.id)


async def test_its_alert_is_labelled_as_a_test(
    worker_ctx: dict[str, Any],
    http: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    get_me(respx_mock, "quake_alert_dev_bot")
    send = respx_mock.post(method_url("sendMessage")).mock(return_value=ok())
    await seed_subscription(db_session, at=JAKARTA, chat_id=31)
    result = await fake(settings(), http, session_factory)

    outcome = await deliver(
        session_factory,
        worker_ctx["telegram"],
        result.delivery_ids[0],
        attempt=1,
        now=datetime.now(UTC),
        config=NotifyConfig.from_settings(worker_ctx["settings"]),
    )

    assert outcome is DeliveryOutcome.SENT
    [message] = sent_messages(send)
    assert message["text"].startswith("[TES - BUKAN GEMPA NYATA]\n")


# --- synthetic rows stay out of everything public ----------------------------------------------


async def test_synthetic_rows_are_excluded_from_every_public_response(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    real = await seed_quake(db_session, occurred_at=DEFAULT_TIME, region="real")
    synthetic = await seed_quake(
        db_session, occurred_at=DEFAULT_TIME + timedelta(hours=1), is_synthetic=True
    )
    lat, lon = JAKARTA

    listed = (await api.get("/v1/earthquakes")).json()
    nearby = (
        await api.get("/v1/earthquakes", params={"lat": lat, "lon": lon, "radius_km": 10})
    ).json()
    latest = (await api.get("/v1/earthquakes/latest")).json()
    detail = await api.get(f"/v1/earthquakes/{synthetic.id}")

    assert [q["id"] for q in listed["data"]] == [str(real.id)]
    assert [q["id"] for q in nearby["data"]] == [str(real.id)]
    assert latest["data"]["id"] == str(real.id)  # though the synthetic one is newer
    assert detail.status_code == 404


async def test_latest_is_404_when_only_synthetic_rows_exist(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_quake(db_session, is_synthetic=True)

    assert (await api.get("/v1/earthquakes/latest")).status_code == 404


@pytest.mark.parametrize("feed", [Feed.GEMPATERKINI, Feed.AUTOGEMPA])
async def test_a_real_report_never_merges_into_a_synthetic_row(
    db_session: AsyncSession, feed: Feed
) -> None:
    # Same time and place: gempaterkini would be a same-feed revision, autogempa a
    # cross-feed exact/fuzzy match, if the row were real.
    lat, lon = JAKARTA
    synthetic = await seed_quake(db_session, at=JAKARTA, feed=Feed.GEMPATERKINI, is_synthetic=True)
    report = make_report(feed, latitude=str(lat), longitude=str(lon), magnitude="5.0")
    config = DedupConfig(
        max_time_diff=timedelta(seconds=60), max_distance_m=50_000, shakemap_base_url=BASE_URL
    )

    result = await upsert_report(db_session, report, config)

    assert result.outcome is UpsertOutcome.INSERTED
    assert result.earthquake_id != synthetic.id
    real = await db_session.get(Earthquake, result.earthquake_id)
    assert real is not None
    assert real.is_synthetic is False
