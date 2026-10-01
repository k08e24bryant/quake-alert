from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app

# Nothing listens on port 1, so both dependencies are unreachable.
UNREACHABLE = Settings(
    environment="test",
    database_url="postgresql+asyncpg://quake:quake@127.0.0.1:1/quake_alert",
    redis_url="redis://127.0.0.1:1/0",
    readiness_timeout_seconds=0.5,
)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(UNREACHABLE)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def test_healthz_does_not_depend_on_database_or_redis(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_returns_503_when_dependencies_are_unreachable(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "checks": {"database": "error", "redis": "error"},
    }
