"""scripts/rotate_webhook_secrets.py against real rows: dry run, batches, idempotence, and
that the old key really can be removed afterwards."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import SecretBox, SecretBoxError
from app.db.models import Subscription, SubscriptionChannel
from app.notifications.webhook import WebhookNotifier, create_webhook_http_client
from app.notifications.webhook_subscriptions import recipient_of
from scripts.rotate_webhook_secrets import rotate_webhook_secrets
from tests.integration.seed import seed_subscription, seed_webhook_subscription
from tests.webhook_samples import (
    FERNET_KEY,
    IP_URL,
    OTHER_FERNET_KEY,
    URL,
    expected_signature,
    posted,
    resolver,
)

OLD_KEY, NEW_KEY = OTHER_FERNET_KEY, FERNET_KEY
OLD = SecretBox([OLD_KEY])
NEW_ONLY = SecretBox([NEW_KEY])
DURING_ROTATION = SecretBox([NEW_KEY, OLD_KEY])  # new key first, old one still listed


def plaintext(n: int) -> str:
    return f"whsec_secret-{n}"


@pytest.fixture
async def seeded(db_session: AsyncSession) -> dict[uuid.UUID, str]:
    """5 secrets under the old key (some pending or inactive: all are rotated), 1 already
    under the new key, plus a Telegram subscription (no secret). Returns id -> plaintext."""
    secrets: dict[uuid.UUID, str] = {}
    for n in range(5):
        row = await seed_webhook_subscription(
            db_session,
            url=URL,
            encrypted_secret=OLD.encrypt(plaintext(n)),
            verified=n != 1,
            is_active=n != 2,
        )
        secrets[row.id] = plaintext(n)
    current = await seed_webhook_subscription(
        db_session, url=URL, encrypted_secret=NEW_ONLY.encrypt(plaintext(99))
    )
    secrets[current.id] = plaintext(99)
    await seed_subscription(db_session)
    return secrets


async def stored(session: AsyncSession) -> dict[uuid.UUID, str]:
    rows = await session.execute(
        select(Subscription.id, Subscription.webhook_secret_encrypted)
        .where(Subscription.channel == SubscriptionChannel.WEBHOOK)
        .execution_options(populate_existing=True)
    )
    return {row_id: token for row_id, token in rows.all() if token is not None}


async def test_dry_run_reports_and_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    seeded: dict[uuid.UUID, str],
) -> None:
    before = await stored(db_session)

    report = await rotate_webhook_secrets(session_factory, DURING_ROTATION, dry_run=True)

    assert (report.checked, report.rotated, report.already_current) == (6, 5, 1)
    assert report.undecryptable == []
    assert "would re-encrypt 5" in report.summary()
    assert await stored(db_session) == before


async def test_rotation_in_batches_is_idempotent_and_frees_the_old_key(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    seeded: dict[uuid.UUID, str],
) -> None:
    first = await rotate_webhook_secrets(session_factory, DURING_ROTATION, batch_size=2)
    after_first = await stored(db_session)
    second = await rotate_webhook_secrets(session_factory, DURING_ROTATION, batch_size=2)

    assert (first.checked, first.rotated, first.already_current) == (6, 5, 1)
    # Idempotent: the second run finds everything current and rewrites nothing.
    assert (second.checked, second.rotated, second.already_current) == (6, 0, 6)
    assert await stored(db_session) == after_first
    # The old key can go: the new key alone decrypts every secret, unchanged.
    assert {row_id: NEW_ONLY.decrypt(token) for row_id, token in after_first.items()} == seeded
    for token in after_first.values():
        with pytest.raises(SecretBoxError):
            OLD.decrypt(token)


async def test_undecryptable_secrets_are_reported_and_left_alone(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    seeded: dict[uuid.UUID, str],
) -> None:
    lost_key = SecretBox(["bG9zdC1rZXktMDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY="])
    broken = await seed_webhook_subscription(
        db_session, url=URL, encrypted_secret=lost_key.encrypt("whsec_lost")
    )
    assert broken.webhook_secret_encrypted is not None

    report = await rotate_webhook_secrets(session_factory, DURING_ROTATION)

    assert report.undecryptable == [broken.id]
    assert report.rotated == 5
    assert str(broken.id) in report.summary()
    assert (await stored(db_session))[broken.id] == broken.webhook_secret_encrypted


@pytest.fixture
async def webhook_http() -> AsyncIterator[httpx.AsyncClient]:
    client = create_webhook_http_client()
    yield client
    await client.aclose()


async def test_alerts_are_signed_with_the_same_secret_after_removing_the_old_key(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    row = await seed_webhook_subscription(
        db_session, url=URL, encrypted_secret=OLD.encrypt("whsec_kept")
    )
    await rotate_webhook_secrets(session_factory, DURING_ROTATION)
    await db_session.refresh(row)
    route = respx_mock.post(IP_URL).respond(204)
    # A worker restarted with only the new key in WEBHOOK_SECRET_KEYS.
    notifier = WebhookNotifier(
        webhook_http,
        NEW_ONLY,
        allow_http=False,
        timeout_seconds=5,
        max_response_bytes=1024,
        resolver=resolver(),
    )

    await notifier.send_test(recipient_of(row))

    [request] = posted(route)
    timestamp = request.headers["x-quake-timestamp"]
    assert request.headers["x-quake-signature"] == expected_signature(
        "whsec_kept", timestamp, request.content
    )
