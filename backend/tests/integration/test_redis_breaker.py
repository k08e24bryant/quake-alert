"""The API's Redis circuit breaker against a real network failure: a TCP proxy in front of
the test Redis that can either forward traffic or "hang" (accept connections, read
commands, never answer). The proxy counts every connection and byte, so "subsequent
requests do not touch Redis" is checked on the wire, not with mocks."""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.circuit_breaker import BreakerState
from app.core.config import Settings
from app.main import create_app
from tests.integration.conftest import IntegrationSettings
from tests.integration.seed import seed_quake

TIMEOUT_SECONDS = 0.2
OPEN_SECONDS = 30.0


class RedisProxy:
    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self.hang = True
        self.connections = 0
        self.bytes_received = 0
        self._writers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None
        self.port = 0

    @property
    def touches(self) -> tuple[int, int]:
        return self.connections, self.bytes_received

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        for writer in list(self._writers):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.add(writer)
        try:
            if self.hang:
                while data := await reader.read(65536):
                    self.bytes_received += len(data)  # swallowed: never answered
                return
            up_reader, up_writer = await asyncio.open_connection(*self.target)
            self._writers.add(up_writer)
            await asyncio.gather(
                self._pipe(reader, up_writer, count=True),
                self._pipe(up_reader, writer, count=False),
                return_exceptions=True,
            )
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _pipe(
        self, source: asyncio.StreamReader, sink: asyncio.StreamWriter, *, count: bool
    ) -> None:
        try:
            while data := await source.read(65536):
                if count:
                    self.bytes_received += len(data)
                sink.write(data)
                await sink.drain()
        finally:
            sink.close()


class FakeClock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def proxy(integration_settings: IntegrationSettings) -> AsyncIterator[RedisProxy]:
    target = make_url(integration_settings.test_redis_url)
    proxy = RedisProxy(target.host or "localhost", target.port or 6379)
    await proxy.start()
    yield proxy
    await proxy.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def breaker_app(
    proxy: RedisProxy,
    clock: FakeClock,
    app_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,  # flushes the test Redis db the proxy forwards to
) -> AsyncIterator[FastAPI]:
    settings = app_settings.model_copy(
        update={
            "redis_url": f"redis://127.0.0.1:{proxy.port}/15",
            "redis_socket_timeout_seconds": TIMEOUT_SECONDS,
            "redis_breaker_failure_threshold": 3,
            "redis_breaker_open_seconds": OPEN_SECONDS,
        }
    )
    app = create_app(settings, breaker_clock=clock)
    async with app.router.lifespan_context(app):
        app.state.sessionmaker = session_factory
        yield app


@pytest.fixture
async def api(breaker_app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await seed_quake(db_session)
    async with AsyncClient(transport=ASGITransport(app=breaker_app), base_url="http://test") as c:
        yield c


def state(app: FastAPI) -> BreakerState:
    breaker_state: BreakerState = app.state.guarded_redis.breaker.state
    return breaker_state


async def open_the_circuit(api: AsyncClient, app: FastAPI) -> None:
    """Request 1: rate limit + cache read time out (2 failures). Request 2: rate limit
    times out (3rd failure) -> open; its cache read is already skipped."""
    for _ in range(2):
        response = await api.get("/v1/earthquakes/latest")
        assert response.status_code == 200
        assert response.headers["X-Cache"] == "BYPASS"
    assert state(app) is BreakerState.OPEN


async def test_hung_redis_opens_the_breaker_and_later_requests_skip_redis(
    api: AsyncClient, breaker_app: FastAPI, proxy: RedisProxy, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="app.core.circuit_breaker")
    await open_the_circuit(api, breaker_app)
    touches_when_opened = proxy.touches
    assert touches_when_opened[0] > 0  # Redis really was tried (and hung) before opening

    for _ in range(10):
        started = time.perf_counter()
        response = await api.get("/v1/earthquakes/latest")
        elapsed = time.perf_counter() - started

        assert response.status_code == 200
        assert response.headers["X-Cache"] == "BYPASS"
        assert "X-RateLimit-Limit" not in response.headers  # rate limiting skipped
        assert elapsed < TIMEOUT_SECONDS  # no Redis timeout paid

    assert proxy.touches == touches_when_opened  # not one connection or byte more
    state_changes = [r for r in caplog.records if r.getMessage() == "circuit breaker state change"]
    assert len(state_changes) == 1  # logged once, not per request


async def test_breaker_recovers_after_the_open_period(
    api: AsyncClient, breaker_app: FastAPI, proxy: RedisProxy, clock: FakeClock
) -> None:
    await open_the_circuit(api, breaker_app)
    proxy.hang = False  # Redis is healthy again

    clock.now += OPEN_SECONDS - 1
    still_open = await api.get("/v1/earthquakes/latest")
    assert still_open.headers["X-Cache"] == "BYPASS"
    assert state(breaker_app) is BreakerState.OPEN

    clock.now += 1
    trial = await api.get("/v1/earthquakes/latest")  # rate-limit call is the trial
    assert state(breaker_app) is BreakerState.CLOSED
    assert trial.headers["X-Cache"] == "MISS"
    assert "X-RateLimit-Limit" in trial.headers

    cached = await api.get("/v1/earthquakes/latest")
    assert cached.headers["X-Cache"] == "HIT"


async def test_readyz_with_hung_redis_is_ready_degraded_and_fast(api: AsyncClient) -> None:
    started = time.perf_counter()
    response = await api.get("/readyz")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert response.json() == {"db": "ok", "redis": "degraded"}
    assert elapsed < TIMEOUT_SECONDS + 0.3  # bounded by the short Redis timeout


async def test_readyz_probes_redis_directly_even_while_the_breaker_is_open(
    api: AsyncClient, breaker_app: FastAPI, proxy: RedisProxy
) -> None:
    await open_the_circuit(api, breaker_app)
    proxy.hang = False  # Redis is back, but the breaker hasn't noticed yet
    before = proxy.touches

    response = await api.get("/readyz")

    assert response.json() == {"db": "ok", "redis": "ok"}  # the truth, not the breaker's view
    assert proxy.touches != before
    assert state(breaker_app) is BreakerState.OPEN  # readyz doesn't feed the breaker


async def test_half_open_failure_reopens_the_breaker(
    api: AsyncClient, breaker_app: FastAPI, proxy: RedisProxy, clock: FakeClock
) -> None:
    await open_the_circuit(api, breaker_app)
    clock.now += OPEN_SECONDS
    before_trial = proxy.touches

    trial = await api.get("/v1/earthquakes/latest")  # Redis still hangs: trial times out

    assert trial.status_code == 200
    assert trial.headers["X-Cache"] == "BYPASS"
    assert state(breaker_app) is BreakerState.OPEN
    after_trial = proxy.touches
    assert after_trial != before_trial  # exactly the trial reached Redis ...

    await api.get("/v1/earthquakes/latest")
    await api.get("/v1/earthquakes")
    assert proxy.touches == after_trial  # ... and nothing after it: reopened for full period
