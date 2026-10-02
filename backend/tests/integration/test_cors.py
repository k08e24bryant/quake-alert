"""CORS on the real app: the frontend's origins may GET the read API; nobody gets CORS
headers anywhere else, and nothing but GET is allowed."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.main import create_app
from tests.integration.seed import seed_quake

FRONTEND = "https://quake.example.com"
LOCAL = "http://localhost:3000"
OTHER = "https://evil.example.net"


@pytest.fixture
async def api(
    app_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[AsyncClient]:
    settings = app_settings.model_copy(update={"cors_allowed_origins": f"{FRONTEND}, {LOCAL}"})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        app.state.sessionmaker = session_factory
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


def preflight(origin: str, method: str = "GET") -> dict[str, str]:
    return {"Origin": origin, "Access-Control-Request-Method": method}


@pytest.mark.parametrize("origin", [FRONTEND, LOCAL])
async def test_allowed_origins_can_read_the_read_api(
    api: AsyncClient, db_session: AsyncSession, origin: str
) -> None:
    quake = await seed_quake(db_session)

    for path in ("/v1/earthquakes", "/v1/earthquakes/latest", f"/v1/earthquakes/{quake.id}"):
        response = await api.get(path, headers={"Origin": origin})
        assert response.status_code == 200, path
        assert response.headers["access-control-allow-origin"] == origin, path
        assert "access-control-allow-credentials" not in response.headers
    status = await api.get("/v1/status", headers={"Origin": origin})
    assert status.headers["access-control-allow-origin"] == origin


async def test_other_origins_get_no_cors_headers(api: AsyncClient) -> None:
    response = await api.get("/v1/earthquakes", headers={"Origin": OTHER})

    # The server still answers (CORS is enforced by the browser), but without the header
    # the browser won't hand the response to the other origin's script.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


async def test_a_similar_looking_origin_is_rejected(api: AsyncClient) -> None:
    for origin in (
        "https://quake.example.com.evil.net",
        "http://quake.example.com",
        FRONTEND + ":8443",
    ):
        response = await api.get("/v1/status", headers={"Origin": origin})
        assert "access-control-allow-origin" not in response.headers, origin


async def test_preflight_allows_get_only(api: AsyncClient) -> None:
    get = await api.options("/v1/earthquakes", headers=preflight(FRONTEND))
    post = await api.options("/v1/earthquakes", headers=preflight(FRONTEND, "POST"))
    other = await api.options("/v1/earthquakes", headers=preflight(OTHER))

    assert get.status_code == 200
    assert get.headers["access-control-allow-origin"] == FRONTEND
    assert get.headers["access-control-allow-methods"] == "GET"
    assert post.status_code == 400
    assert other.status_code == 400
    assert "access-control-allow-origin" not in other.headers


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/readyz"),
        ("GET", "/healthz"),
        ("POST", "/v1/subscriptions/webhook"),
        ("DELETE", "/v1/subscriptions/webhook/0b6f2a4e-3c1d-4f7e-9a51-2d8c6e0f4b13"),
        ("POST", "/v1/telegram/webhook"),
    ],
)
async def test_nothing_else_gets_cors_even_from_an_allowed_origin(
    api: AsyncClient, method: str, path: str
) -> None:
    response = await api.request(method, path, headers={"Origin": FRONTEND})
    options = await api.options(path, headers=preflight(FRONTEND, method))

    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-origin" not in options.headers


async def test_no_origins_configured_means_no_cors(
    app_settings: Settings, session_factory: async_sessionmaker[AsyncSession], redis_client: Redis
) -> None:
    app = create_app(app_settings)  # cors_allowed_origins defaults to ""
    async with app.router.lifespan_context(app):
        app.state.sessionmaker = session_factory
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/v1/status", headers={"Origin": FRONTEND})
    assert "access-control-allow-origin" not in response.headers


def test_production_with_a_wildcard_refuses_to_start(app_settings: Settings) -> None:
    settings = app_settings.model_copy(
        update={
            "environment": "production",
            "cors_allowed_origins": "*",
            "webhook_secret_keys": SecretStr("dGVzdC1rZXktMDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY="),
        }
    )
    with pytest.raises(ValueError, match="production"):
        create_app(settings)
