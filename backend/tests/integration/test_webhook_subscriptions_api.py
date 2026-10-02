import hashlib
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import Float, delete, func, select
from sqlalchemy.engine import URL as DatabaseURL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.circuit_breaker import BreakerState
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.db.models import NotificationDelivery, Subscription
from app.main import create_app
from app.notifications.webhook import WebhookNotifier, create_webhook_http_client
from tests.integration.conftest import IntegrationSettings
from tests.integration.seed import seed_quake, seed_subscription, seed_webhook_subscription
from tests.webhook_samples import (
    FERNET_KEY,
    HOST,
    IP_URL,
    PUBLIC_IP,
    URL,
    FakeReceiver,
    expected_signature,
    payload,
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
def receiver(respx_mock: respx.MockRouter) -> FakeReceiver:
    """The receiver at URL (mocked HTTP): echoes verification challenges, 204 otherwise."""
    fake = FakeReceiver()
    respx_mock.post(IP_URL).mock(side_effect=fake)
    return fake


@pytest.fixture
def webhooks_enabled(
    api_app: FastAPI, webhook_http: httpx.AsyncClient, receiver: FakeReceiver
) -> None:
    """The app's webhook notifier with a test key, fake DNS and a mocked receiver: nothing
    here reaches the network."""
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
    assert (body["radius_km"], body["min_magnitude"]) == (200, 4.5)
    # The receiver echoed the challenge while the subscription was created.
    assert (body["status"], body["is_active"]) == ("active", True)
    assert body["verification"] == {"verified": True, "status_code": 200, "error": None}
    row = await db_session.get(Subscription, uuid.UUID(body["id"]))
    assert row is not None
    assert (row.is_active, row.verified_at is not None) == (True, True)
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
async def test_test_payloads_share_the_write_budget(api: AsyncClient) -> None:
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
    api: AsyncClient, receiver: FakeReceiver
) -> None:
    created = (await create(api)).json()

    response = await api.post(
        f"{ENDPOINT}/{created['id']}/test", headers=bearer(created["manage_token"])
    )

    assert response.json() == {"delivered": True, "status_code": 204, "error": None}
    _, request = receiver.requests  # after the verification request
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
    api: AsyncClient, receiver: FakeReceiver
) -> None:
    receiver.status, receiver.text = 500, "boom"
    created = (await create(api)).json()

    response = await api.post(
        f"{ENDPOINT}/{created['id']}/test", headers=bearer(created["manage_token"])
    )

    assert response.json() == {"delivered": False, "status_code": None, "error": "HTTP 500: boom"}


@pytest.mark.usefixtures("webhooks_enabled")
async def test_test_endpoint_needs_the_manage_token(
    api: AsyncClient, receiver: FakeReceiver
) -> None:
    created = (await create(api)).json()

    response = await api.post(f"{ENDPOINT}/{created['id']}/test", headers=bearer("qamt_wrong"))

    assert response.status_code == 404
    assert receiver.events() == ["webhook.verification"]  # nothing after it


@pytest.mark.usefixtures("webhooks_enabled")
async def test_test_endpoint_refuses_a_pending_subscription(
    api: AsyncClient, receiver: FakeReceiver
) -> None:
    receiver.verification = lambda _: httpx.Response(401)
    created = (await create(api)).json()

    response = await api.post(
        f"{ENDPOINT}/{created['id']}/test", headers=bearer(created["manage_token"])
    )

    assert response.status_code == 409
    assert "pending verification" in response.json()["detail"]
    assert receiver.events() == ["webhook.verification"]


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


# --- ownership verification -----------------------------------------------------------------


async def row_state(session: AsyncSession, subscription_id: str) -> tuple[bool, bool]:
    """(is_active, verified)"""
    row = await session.get(Subscription, uuid.UUID(subscription_id), populate_existing=True)
    assert row is not None
    return row.is_active, row.verified_at is not None


@pytest.mark.usefixtures("webhooks_enabled")
async def test_verification_request_is_signed_and_carries_a_fresh_challenge(
    api: AsyncClient, receiver: FakeReceiver
) -> None:
    created = (await create(api)).json()

    [request] = receiver.requests
    # The same send path as alerts: the vetted IP, the subscriber's Host, a signature.
    assert request.url.host == PUBLIC_IP
    assert request.headers["host"] == HOST
    signature = expected_signature(
        created["signing_secret"], request.headers["x-quake-timestamp"], request.content
    )
    assert request.headers["x-quake-signature"] == signature
    body = payload(request)
    assert (body["event"], body["test"], body["synthetic"], body["earthquake"]) == (
        "webhook.verification",
        True,
        False,
        None,
    )
    assert len(body["challenge"]) >= 40  # 256 random bits, urlsafe base64
    await create(api)
    assert payload(receiver.requests[1])["challenge"] != body["challenge"]


@pytest.mark.usefixtures("webhooks_enabled")
@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (lambda b: httpx.Response(200, json={"challenge": "not-it"}), "challenge"),
        (lambda b: httpx.Response(200, json={"challenge": b["challenge"][:-1]}), "challenge"),
        (lambda b: httpx.Response(200, json={}), "challenge"),
        (lambda b: httpx.Response(200, json=[b["challenge"]]), "challenge"),
        (lambda b: httpx.Response(200, text=b["challenge"]), "challenge"),  # not JSON
        (lambda b: httpx.Response(204), "challenge"),
        (lambda b: httpx.Response(401, json={"challenge": b["challenge"]}), "HTTP 401"),
        (lambda b: httpx.Response(429, json={"challenge": b["challenge"]}), "HTTP 429"),
        (lambda b: httpx.Response(500), "HTTP 500"),
        (lambda b: httpx.Response(302, headers={"Location": "/elsewhere"}), "redirect"),
    ],
)
async def test_wrong_answer_leaves_the_subscription_pending(
    api: AsyncClient,
    db_session: AsyncSession,
    receiver: FakeReceiver,
    answer: Callable[[dict[str, Any]], httpx.Response],
    error: str,
) -> None:
    receiver.verification = answer

    response = await create(api)

    assert response.status_code == 201  # created all the same: the owner can retry
    body = response.json()
    assert (body["status"], body["is_active"]) == ("pending_verification", False)
    assert body["verification"]["verified"] is False
    assert error in body["verification"]["error"]
    assert body["manage_token"] and body["signing_secret"]
    assert await row_state(db_session, body["id"]) == (False, False)
    assert len(receiver.requests) == 1  # one attempt, never retried by itself


@pytest.mark.usefixtures("webhooks_enabled")
async def test_verification_timeout_leaves_the_subscription_pending(
    api: AsyncClient, db_session: AsyncSession, receiver: FakeReceiver
) -> None:
    def too_slow(_: dict[str, Any]) -> httpx.Response:
        raise httpx.ReadTimeout("no answer in time")

    receiver.verification = too_slow

    body = (await create(api)).json()

    assert body["status"] == "pending_verification"
    assert body["verification"] == {"verified": False, "status_code": None, "error": "timeout"}
    assert await row_state(db_session, body["id"]) == (False, False)


async def test_verification_goes_through_the_ssrf_check_at_send_time(
    api: AsyncClient,
    api_app: FastAPI,
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    # DNS rebinding: public when the URL is checked, internal a moment later when the
    # verification request is sent. The send-time check refuses it.
    answers = iter([[PUBLIC_IP], ["127.0.0.1"]])

    async def rebinding(host: str, port: int) -> list[str]:
        return next(answers)

    anything = respx_mock.route().respond(200)
    api_app.state.webhook_notifier = WebhookNotifier(
        webhook_http,
        secret_box(),
        allow_http=False,
        timeout_seconds=5,
        max_response_bytes=65536,
        resolver=rebinding,
    )

    body = (await create(api)).json()

    assert not anything.called
    assert body["status"] == "pending_verification"
    assert "unsafe webhook target" in body["verification"]["error"]
    assert await row_state(db_session, body["id"]) == (False, False)


@pytest.mark.usefixtures("webhooks_enabled")
async def test_verify_retry_activates_a_pending_subscription(
    api: AsyncClient, db_session: AsyncSession, receiver: FakeReceiver
) -> None:
    receiver.verification = lambda _: httpx.Response(401)  # doesn't know the secret yet
    created = (await create(api)).json()
    receiver.verification = None  # now it does

    response = await api.post(
        f"{ENDPOINT}/{created['id']}/verify", headers=bearer(created["manage_token"])
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "active",
        "verification": {"verified": True, "status_code": 200, "error": None},
    }
    assert await row_state(db_session, created["id"]) == (True, True)
    assert receiver.events() == ["webhook.verification", "webhook.verification"]


@pytest.mark.usefixtures("webhooks_enabled")
async def test_failed_verify_retry_keeps_it_pending(
    api: AsyncClient, db_session: AsyncSession, receiver: FakeReceiver
) -> None:
    receiver.verification = lambda _: httpx.Response(200, json={"challenge": "wrong"})
    created = (await create(api)).json()

    response = await api.post(
        f"{ENDPOINT}/{created['id']}/verify", headers=bearer(created["manage_token"])
    )

    assert response.status_code == 200
    assert response.json()["status"] == "pending_verification"
    assert response.json()["verification"]["verified"] is False
    assert await row_state(db_session, created["id"]) == (False, False)


@pytest.mark.usefixtures("webhooks_enabled")
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer qamt_wrong"}])
async def test_verify_needs_the_manage_token(
    api: AsyncClient, receiver: FakeReceiver, headers: dict[str, str]
) -> None:
    receiver.verification = lambda _: httpx.Response(401)
    created = (await create(api)).json()
    receiver.verification = None

    response = await api.post(f"{ENDPOINT}/{created['id']}/verify", headers=headers)
    unknown = await api.post(
        f"{ENDPOINT}/{uuid.uuid4()}/verify", headers=bearer(created["manage_token"])
    )

    assert (response.status_code, response.json()) == (404, unknown.json())
    assert len(receiver.requests) == 1  # only the creation attempt


@pytest.mark.usefixtures("webhooks_enabled")
async def test_verify_never_reactivates_and_sends_nothing_unless_pending(
    api: AsyncClient, db_session: AsyncSession, receiver: FakeReceiver
) -> None:
    created = (await create(api)).json()
    verify = f"{ENDPOINT}/{created['id']}/verify"

    already = await api.post(verify, headers=bearer(created["manage_token"]))
    # Deactivated, e.g. by a 410 or by consecutive failures.
    row = await db_session.get(Subscription, uuid.UUID(created["id"]))
    assert row is not None
    row.is_active = False
    await db_session.flush()
    inactive = await api.post(verify, headers=bearer(created["manage_token"]))

    assert (already.status_code, already.json()["detail"]) == (
        409,
        "Subscription is already active.",
    )
    assert inactive.status_code == 409
    assert "never reactivated" in inactive.json()["detail"]
    assert await row_state(db_session, created["id"]) == (False, True)
    assert len(receiver.requests) == 1  # only the creation attempt


@pytest.mark.usefixtures("webhooks_enabled")
async def test_verify_retries_share_the_write_budget(
    api: AsyncClient, receiver: FakeReceiver
) -> None:
    receiver.verification = lambda _: httpx.Response(401)
    created = (await create(api)).json()
    verify = f"{ENDPOINT}/{created['id']}/verify"
    for _ in range(4):
        assert (await api.post(verify, headers=bearer(created["manage_token"]))).status_code == 200

    limited = await api.post(verify, headers=bearer(created["manage_token"]))

    assert limited.status_code == 429
    assert len(receiver.requests) == 5  # the limited call sent nothing


# --- writes fail closed without the rate limiter --------------------------------------------


@pytest.fixture
async def api_without_redis(
    app_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    webhook_http: httpx.AsyncClient,
    receiver: FakeReceiver,
) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    settings = app_settings.model_copy(
        update={
            "redis_url": "redis://127.0.0.1:1/0",
            "redis_socket_timeout_seconds": 0.2,
            "redis_breaker_open_seconds": 30.0,
        }
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        app.state.sessionmaker = session_factory
        app.state.webhook_notifier = WebhookNotifier(
            webhook_http,
            secret_box(),
            allow_http=False,
            timeout_seconds=5,
            max_response_bytes=65536,
            resolver=resolver(),
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


async def test_writes_answer_503_without_the_rate_limiter_while_reads_still_work(
    api_without_redis: tuple[AsyncClient, FastAPI],
    db_session: AsyncSession,
    receiver: FakeReceiver,
) -> None:
    api, app = api_without_redis
    token = "qamt_existing"  # noqa: S105  (fake)
    existing = await seed_webhook_subscription(
        db_session,
        url=URL,
        encrypted_secret=secret_box().encrypt("whsec_x"),
        manage_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        verified=False,
    )
    writes: list[tuple[str, str, dict[str, Any]]] = [
        ("POST", ENDPOINT, {"json": VALID}),
        ("POST", f"{ENDPOINT}/{existing.id}/verify", {"headers": bearer(token)}),
        ("POST", f"{ENDPOINT}/{existing.id}/test", {"headers": bearer(token)}),
        ("DELETE", f"{ENDPOINT}/{existing.id}", {"headers": bearer(token)}),
    ]

    # Twice round: the first calls fail on the dead socket, later ones on the open breaker.
    for method, url, kwargs in writes * 2:
        response = await api.request(method, url, **kwargs)
        assert response.status_code == 503, (method, url)
        assert response.headers["Retry-After"] == "30"
        assert "Rate limiting is unavailable" in response.json()["detail"]
    assert app.state.guarded_redis.breaker.state is BreakerState.OPEN

    # Nothing was written or sent.
    assert receiver.requests == []
    count = select(func.count()).select_from(Subscription)
    assert await db_session.scalar(count) == 1
    # Reads keep failing open.
    listed = await api.get("/v1/earthquakes")
    assert listed.status_code == 200
    assert "X-RateLimit-Limit" not in listed.headers


async def test_verify_reports_not_found_if_deleted_while_waiting_on_the_receiver(
    api: AsyncClient,
    api_app: FastAPI,
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    token = "qamt_race"  # noqa: S105  (fake)
    row = await seed_webhook_subscription(
        db_session,
        url=URL,
        encrypted_secret=secret_box().encrypt("whsec_x"),
        manage_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        verified=False,
    )

    async def echo_after_deletion(request: httpx.Request) -> httpx.Response:
        # The owner deletes the subscription while its receiver is still answering.
        await db_session.execute(delete(Subscription).where(Subscription.id == row.id))
        return httpx.Response(200, json={"challenge": payload(request)["challenge"]})

    respx_mock.post(IP_URL).mock(side_effect=echo_after_deletion)
    api_app.state.webhook_notifier = WebhookNotifier(
        webhook_http,
        secret_box(),
        allow_http=False,
        timeout_seconds=5,
        max_response_bytes=65536,
        resolver=resolver(),
    )

    response = await api.post(f"{ENDPOINT}/{row.id}/verify", headers=bearer(token))

    assert (response.status_code, response.json()) == (404, {"detail": "Subscription not found."})
    count = select(func.count()).select_from(Subscription)
    assert await db_session.scalar(count) == 0  # not resurrected
