from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.rate_limit import RateLimiter
from app.earthquakes.cache import LATEST_KEY, LIST_KEY_PREFIX, invalidate_latest
from app.main import create_app
from tests.bmkg_samples import DEFAULT_TIME
from tests.integration.seed import JAKARTA, seed_quake

# --- cache --------------------------------------------------------------------------------


async def test_latest_is_cached_until_ingestion_invalidates_it(
    api: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    await seed_quake(db_session, region="first")

    miss = await api.get("/v1/earthquakes/latest")
    hit = await api.get("/v1/earthquakes/latest")
    assert (miss.headers["X-Cache"], hit.headers["X-Cache"]) == ("MISS", "HIT")
    assert hit.json() == miss.json()

    # A newer quake lands without invalidation: the cache still answers (proves it is used).
    await seed_quake(db_session, region="newer", occurred_at=DEFAULT_TIME + timedelta(minutes=1))
    stale = await api.get("/v1/earthquakes/latest")
    assert (stale.headers["X-Cache"], stale.json()["data"]["region"]) == ("HIT", "first")

    await invalidate_latest(redis_client)  # what ingestion does after inserting/updating
    fresh = await api.get("/v1/earthquakes/latest")
    assert (fresh.headers["X-Cache"], fresh.json()["data"]["region"]) == ("MISS", "newer")


async def test_latest_cache_has_a_ttl(
    api: AsyncClient, db_session: AsyncSession, redis_client: Redis, app_settings: Settings
) -> None:
    await seed_quake(db_session)
    await api.get("/v1/earthquakes/latest")

    assert 0 < await redis_client.ttl(LATEST_KEY) <= app_settings.cache_latest_ttl_seconds


async def test_list_cache_is_keyed_by_normalized_params(
    api: AsyncClient, db_session: AsyncSession, redis_client: Redis, app_settings: Settings
) -> None:
    await seed_quake(db_session)
    lat, lon = JAKARTA

    first = await api.get(
        "/v1/earthquakes", params={"lat": f"{lat}", "lon": f"{lon}", "radius_km": "50"}
    )
    # Same query: other parameter order, other spellings of the same numbers and limit.
    same = await api.get(
        "/v1/earthquakes",
        params={"radius_km": "50.0", "lon": f"{lon}0", "lat": f"{lat}00", "limit": "20"},
    )
    other = await api.get("/v1/earthquakes", params={"lat": lat, "lon": lon, "radius_km": 51})

    assert [r.headers["X-Cache"] for r in (first, same, other)] == ["MISS", "HIT", "MISS"]
    assert same.json() == first.json()
    keys = [key async for key in redis_client.scan_iter(f"{LIST_KEY_PREFIX}*")]
    assert len(keys) == 2
    for key in keys:
        assert 0 < await redis_client.ttl(key) <= app_settings.cache_list_ttl_seconds


async def test_datetime_filters_in_different_offsets_share_a_cache_entry(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_quake(db_session)

    utc = await api.get("/v1/earthquakes", params={"start": "2026-10-01T00:00:00+00:00"})
    wib = await api.get("/v1/earthquakes", params={"start": "2026-10-01T07:00:00+07:00"})

    assert (utc.headers["X-Cache"], wib.headers["X-Cache"]) == ("MISS", "HIT")


# --- Redis down ---------------------------------------------------------------------------


@pytest.fixture
async def api_without_redis(
    app_settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncClient]:
    settings = app_settings.model_copy(
        update={"redis_url": "redis://127.0.0.1:1/0", "redis_socket_timeout_seconds": 0.2}
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        app.state.sessionmaker = session_factory
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


async def test_redis_down_serves_from_the_database(
    api_without_redis: AsyncClient, db_session: AsyncSession
) -> None:
    row = await seed_quake(db_session)

    listed = await api_without_redis.get("/v1/earthquakes")
    latest = await api_without_redis.get("/v1/earthquakes/latest")
    detail = await api_without_redis.get(f"/v1/earthquakes/{row.id}")

    assert [r.status_code for r in (listed, latest, detail)] == [200, 200, 200]
    assert listed.headers["X-Cache"] == latest.headers["X-Cache"] == "BYPASS"
    assert latest.json()["data"]["id"] == str(row.id)
    # The rate limiter fails open too: no limit headers, no 429.
    assert "X-RateLimit-Limit" not in listed.headers


async def test_redis_down_is_reported_by_readyz_but_not_healthz(
    api_without_redis: AsyncClient,
) -> None:
    assert (await api_without_redis.get("/healthz")).status_code == 200
    assert (await api_without_redis.get("/readyz")).status_code == 503


# --- rate limiting ------------------------------------------------------------------------

WINDOW_START = 1_800_000_000.0  # divisible by 60: a window starts exactly here


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(api_app: FastAPI) -> FakeClock:
    """Limit /v1 to 3 requests per minute with a controllable clock."""
    fake = FakeClock(WINDOW_START + 15)
    api_app.state.rate_limiter = RateLimiter(api_app.state.guarded_redis, limit=3, clock=fake)
    return fake


async def test_requests_over_the_limit_get_429_with_retry_after(
    api: AsyncClient, clock: FakeClock
) -> None:
    responses = [await api.get("/v1/earthquakes") for _ in range(4)]

    assert [r.status_code for r in responses] == [200, 200, 200, 429]
    assert [r.headers["X-RateLimit-Remaining"] for r in responses] == ["2", "1", "0", "0"]
    limited = responses[-1]
    assert limited.headers["X-RateLimit-Limit"] == "3"
    assert limited.headers["Retry-After"] == "45"  # 15 s into a 60 s window
    assert limited.headers["X-RateLimit-Reset"] == "45"


async def test_limit_resets_in_the_next_window(api: AsyncClient, clock: FakeClock) -> None:
    for _ in range(4):
        await api.get("/v1/earthquakes")

    clock.now = WINDOW_START + 60
    response = await api.get("/v1/earthquakes")

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Remaining"] == "2"


async def test_health_endpoints_are_not_rate_limited(api: AsyncClient, clock: FakeClock) -> None:
    for _ in range(4):
        await api.get("/v1/earthquakes")

    assert (await api.get("/healthz")).status_code == 200
    assert "X-RateLimit-Limit" not in (await api.get("/healthz")).headers


async def test_forwarded_for_is_ignored_unless_trusted(api: AsyncClient, clock: FakeClock) -> None:
    for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
        await api.get("/v1/earthquakes", headers={"X-Forwarded-For": ip})

    # Untrusted: every request counted against the socket peer, whatever the header says.
    response = await api.get("/v1/earthquakes", headers={"X-Forwarded-For": "203.0.113.4"})
    assert response.status_code == 429


async def test_trusted_forwarded_for_limits_each_client_separately(
    api: AsyncClient, api_app: FastAPI, clock: FakeClock
) -> None:
    api_app.state.settings = api_app.state.settings.model_copy(update={"trust_proxy_headers": True})
    for _ in range(3):
        await api.get("/v1/earthquakes", headers={"X-Forwarded-For": "203.0.113.1"})

    blocked = await api.get("/v1/earthquakes", headers={"X-Forwarded-For": "203.0.113.1"})
    other_client = await api.get("/v1/earthquakes", headers={"X-Forwarded-For": "203.0.113.2"})
    # A client can prepend anything; only the entry our proxy appended (the last) counts.
    spoofed = await api.get(
        "/v1/earthquakes", headers={"X-Forwarded-For": "198.51.100.9, 203.0.113.1"}
    )

    assert (blocked.status_code, other_client.status_code, spoofed.status_code) == (429, 200, 429)
