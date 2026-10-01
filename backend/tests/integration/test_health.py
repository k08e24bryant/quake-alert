from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


async def test_healthz_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_every_component_ok(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"db": "ok", "redis": "ok"}


async def test_readyz_is_503_when_the_database_is_down_even_with_redis_up(
    app_settings: Settings,
) -> None:
    settings = app_settings.model_copy(
        update={
            "database_url": "postgresql+asyncpg://quake:quake@127.0.0.1:1/quake_alert",
            "readiness_timeout_seconds": 0.5,
        }
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"db": "error", "redis": "ok"}
