import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import Float, func, select
from sqlalchemy.engine import URL as DatabaseURL
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import SecretBox
from app.db.models import NotificationDelivery, Subscription
from app.main import create_app
from app.notifications.webhook import WebhookNotifier, create_webhook_http_client
from tests.integration.conftest import IntegrationSettings
from tests.integration.seed import seed_quake, seed_subscription
from tests.webhook_samples import (
    FERNET_KEY,
    HOST,
    IP_URL,
    URL,
    expected_signature,
    payload,
    posted,
    resolver,
    secret_box,
)

ENDPOINT = "/v1/subscriptions/webhook"
VALID = {"url": URL, "lat": -6.208763, "lon": 106.845599, "radius_km": 200, "min_magnitude": 4.5}


@pytest.fixture
async def webhook_http() -> AsyncIterator[httpx.AsyncClient]:
    client = create_webhook_http_client()
    yield client
    await client.aclose()


@pytest.fixture
def webhooks_enabled(api_app: FastAPI, webhook_http: httpx.AsyncClient) -> None:
    """The app's webhook notifier with a test key and fake DNS (no network)."""
    api_app.state.webhook_notifier = WebhookNotifier(
        webhook_http,
        secret_box(),
        allow_http=False,
        timeout_seconds=5,
        max_response_bytes=65536,
        resolver=resolver({"internal.example": ["10.0.0.7"], "meta.example": ["169.254.169.254"]}),
    )


async def create(api: AsyncClient, **overrides: Any) -> httpx.Response:
    return await api.post(ENDPOINT, json={**VALID, **overrides})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- create ---------------------------------------------------------------------------------


@pytest.mark.usefixtures("webhooks_enabled")
async def test_create_returns_credentials_once_and_stores_them_safely(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    response = await create(api)

    assert response.status_code == 201
    body = response.json()
    assert body["signing_secret"].startswith("whsec_")
    assert body["manage_token"].startswith("qamt_")
    assert (body["latitude"], body["longitude"]) == (-6.21, 106.85)  # rounded, ~1 km
    assert (body["radius_km"], body["min_magnitude"], body["is_active"]) == (200, 4.5, True)
    row = await db_session.get(Subscription, uuid.UUID(body["id"]))
    assert row is not None
    assert row.webhook_secret_encrypted is not None
    # The secret is encrypted (recoverable for signing), the token only hashed.
    assert body["signing_secret"] not in row.webhook_secret_encrypted
    assert secret_box().decrypt(row.webhook_secret_encrypted) == body["signing_secret"]
    assert row.manage_token_hash == hashlib.sha256(body["manage_token"].encode()).hexdigest()
    lat, lon = (
        await db_session.execute(
            select(
                func.ST_Y(func.geometry(Subscription.location), type_=Float),
                func.ST_X(func.geometry(Subscription.location), type_=Float),
            ).where(Subscription.id == row.id)
        )
    ).one()
    assert (lat, lon) == (-6.21, 106.85)


@pytest.mark.usefixtures("webhooks_enabled")
@pytest.mark.parametrize(
    "overrides",
    [
        {"radius_km": 9},
        {"radius_km": 1001},
        {"radius_km": 150.5},
        {"min_magnitude": 1.9},
        {"min_magnitude": 9.1},
        {"min_magnitude": 4.55},
        {"lat": 91},
        {"lon": -181},
        {"url": "x" * 2049},
        {"unexpected": True},
    ],
)
async def test_create_validation(api: AsyncClient, overrides: dict[str, Any]) -> None:
    assert (await create(api, **overrides)).status_code == 422


@pytest.mark.usefixtures("webhooks_enabled")
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://hooks.example.com/x", "scheme"),
        ("https://user:pw@hooks.example.com/x", "credentials"),
        ("https://127.0.0.1/x", "loopback"),
        ("https://[::1]/x", "loopback"),
        ("https://internal.example/x", "private"),
        ("https://meta.example/x", "cloud metadata"),
        ("https://unresolvable.example/x", "cannot resolve"),
    ],
)
async def test_create_rejects_unsafe_urls(
    api: AsyncClient, db_session: AsyncSession, url: str, reason: str
) -> None:
    response = await create(api, url=url)

    assert response.status_code == 422
    assert reason in response.json()["detail"]
    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 0


async def test_create_without_configured_keys_is_unavailable(
    api: AsyncClient, api_app: FastAPI
) -> None:
    assert api_app.state.webhook_notifier.secret_box is None  # app_settings has no keys

    assert (await create(api)).status_code == 503


# --- delete ---------------------------------------------------------------------------------


@pytest.mark.usefixtures("webhooks_enabled")
async def test_delete_with_the_manage_token(api: AsyncClient, db_session: AsyncSession) -> None:
    created = (await create(api)).json()
    subscription_id = uuid.UUID(created["id"])
    quake = await seed_quake(db_session)
    db_session.add(NotificationDelivery(subscription_id=subscription_id, earthquake_id=quake.id))
    await db_session.flush()

    response = await api.delete(
        f"{ENDPOINT}/{subscription_id}", headers=bearer(created["manage_token"])
    )

    assert response.status_code == 204
    assert await db_session.get(Subscription, subscription_id) is None
    deliveries = select(func.count()).select_from(NotificationDelivery)
    assert await db_session.scalar(deliveries) == 0
    again = await api.delete(
        f"{ENDPOINT}/{subscription_id}", headers=bearer(created["manage_token"])
    )
    assert again.status_code == 404


@pytest.mark.usefixtures("webhooks_enabled")
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer qamt_wrong"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
    ],
)
async def test_wrong_or_missing_token_is_404_and_deletes_nothing(
    api: AsyncClient, db_session: AsyncSession, headers: dict[str, str]
) -> None:
    created = (await create(api)).json()

    response = await api.delete(f"{ENDPOINT}/{created['id']}", headers=headers)

    assert response.status_code == 404
    assert response.json() == {"detail": "Subscription not found."}
    assert await db_session.get(Subscription, uuid.UUID(created["id"])) is not None


@pytest.mark.usefixtures("webhooks_enabled")
async def test_unknown_id_looks_exactly_like_a_wrong_token(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    created = (await create(api)).json()
    telegram = await seed_subscription(db_session)

    unknown = await api.delete(
        f"{ENDPOINT}/{uuid.uuid4()}", headers=bearer(created["manage_token"])
    )
    wrong = await api.delete(f"{ENDPOINT}/{created['id']}", headers=bearer("qamt_wrong"))
    not_a_webhook = await api.delete(
        f"{ENDPOINT}/{telegram.id}", headers=bearer(created["manage_token"])
    )

    for response in (unknown, wrong, not_a_webhook):
        assert (response.status_code, response.json()) == (404, wrong.json())


# --- the stricter hourly limit ------------------------------------------------------------


@pytest.mark.usefixtures("webhooks_enabled")
async def test_creating_is_limited_per_ip_per_hour(api: AsyncClient) -> None:
    for _ in range(5):
        assert (await create(api)).status_code == 201

    limited = await create(api)

    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 60  # the hourly window, not the minute one
    assert limited.headers["X-RateLimit-Limit"] == "5"
    # Only subscription writes are limited this tightly.
    assert (await api.get("/v1/earthquakes")).status_code == 200


@pytest.mark.usefixtures("webhooks_enabled")
async def test_test_payloads_share_the_write_budget(
    api: AsyncClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(IP_URL).respond(200)
    created = (await create(api)).json()
    test_url = f"{ENDPOINT}/{created['id']}/test"
    for _ in range(4):
        assert (
            await api.post(test_url, headers=bearer(created["manage_token"]))
        ).status_code == 200

    assert (await api.post(test_url, headers=bearer(created["manage_token"]))).status_code == 429


# --- test endpoint --------------------------------------------------------------------------


@pytest.mark.usefixtures("webhooks_enabled")
async def test_test_endpoint_sends_a_signed_test_payload(
    api: AsyncClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(IP_URL).respond(204)
    created = (await create(api)).json()

    response = await api.post(
        f"{ENDPOINT}/{created['id']}/test", headers=bearer(created["manage_token"])
    )

    assert response.json() == {"delivered": True, "status_code": 204, "error": None}
    [request] = posted(route)
    assert request.headers["host"] == HOST
    signature = expected_signature(
        created["signing_secret"], request.headers["x-quake-timestamp"], request.content
    )
    assert request.headers["x-quake-signature"] == signature
    body = payload(request)
    assert (body["event"], body["test"], body["synthetic"], body["earthquake"]) == (
        "webhook.test",
        True,
        False,
        None,
    )


@pytest.mark.usefixtures("webhooks_enabled")
async def test_test_endpoint_reports_a_failing_receiver(
    api: AsyncClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(IP_URL).respond(500, text="boom")
    created = (await create(api)).json()

    response = await api.post(
        f"{ENDPOINT}/{created['id']}/test", headers=bearer(created["manage_token"])
    )

    assert response.json() == {"delivered": False, "status_code": None, "error": "HTTP 500: boom"}


@pytest.mark.usefixtures("webhooks_enabled")
async def test_test_endpoint_needs_the_manage_token(
    api: AsyncClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(IP_URL).respond(200)
    created = (await create(api)).json()

    response = await api.post(f"{ENDPOINT}/{created['id']}/test", headers=bearer("qamt_wrong"))

    assert response.status_code == 404
    assert not route.called


# --- startup --------------------------------------------------------------------------------


async def test_production_refuses_to_start_without_webhook_keys(
    database_url: DatabaseURL, integration_settings: IntegrationSettings
) -> None:
    def production(keys: str) -> Settings:
        return Settings(
            environment="production",
            database_url=database_url.render_as_string(hide_password=False),
            redis_url=integration_settings.test_redis_url,
            webhook_secret_keys=SecretStr(keys),
        )

    without_keys = create_app(production(""))
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET_KEYS"):
        async with without_keys.router.lifespan_context(without_keys):
            pass

    with_key = create_app(production(FERNET_KEY))
    async with with_key.router.lifespan_context(with_key):
        assert isinstance(with_key.state.webhook_notifier.secret_box, SecretBox)
