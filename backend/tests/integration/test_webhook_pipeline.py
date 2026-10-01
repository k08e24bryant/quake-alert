"""One quake, one Telegram and one webhook subscriber: the same outbox, matching and
delivery jobs serve both channels."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DeliveryStatus, NotificationDelivery
from app.notifications.dispatcher import NotifyConfig
from app.notifications.webhook import WebhookNotifier, create_webhook_http_client
from tests.integration.seed import JAKARTA, seed_subscription, seed_webhook_subscription
from tests.integration.test_notification_pipeline import drain, fresh_autogempa, serve_bmkg
from tests.telegram_samples import method_url, ok, sent_messages
from tests.webhook_samples import IP_URL, URL, payload, posted, resolver, secret_box
from worker.jobs import poll_bmkg_feeds


@pytest.fixture
async def ctx(worker_ctx: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    http = create_webhook_http_client()
    webhook = WebhookNotifier(
        http,
        secret_box(),
        allow_http=False,
        timeout_seconds=5,
        max_response_bytes=65536,
        resolver=resolver(),
    )
    yield {**worker_ctx, "webhook": webhook}
    await http.aclose()


async def test_one_quake_reaches_both_channels_exactly_once(
    ctx: dict[str, Any], db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    assert isinstance(ctx["notify_config"], NotifyConfig)
    telegram_route = respx_mock.post(method_url("sendMessage")).mock(return_value=ok())
    webhook_route = respx_mock.post(IP_URL).respond(200)
    await seed_subscription(db_session, at=JAKARTA, chat_id=1001)
    hook = await seed_webhook_subscription(
        db_session, url=URL, encrypted_secret=secret_box().encrypt("whsec_x"), at=JAKARTA
    )
    occurred_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=3)
    serve_bmkg(respx_mock, fresh_autogempa("4.6", occurred_at))

    await poll_bmkg_feeds(ctx)
    await drain(ctx)
    await poll_bmkg_feeds(ctx)  # nothing new: no second alert on either channel
    await drain(ctx)

    assert [m["chat_id"] for m in sent_messages(telegram_route)] == [1001]
    [request] = posted(webhook_route)
    body = payload(request)
    assert (body["event"], body["earthquake"]["magnitude"]) == ("earthquake.alert", 4.6)
    statuses = (
        await db_session.execute(
            select(NotificationDelivery.subscription_id, NotificationDelivery.status)
        )
    ).all()
    assert sorted(status for _, status in statuses) == [DeliveryStatus.SENT] * 2
    assert hook.id in {subscription_id for subscription_id, _ in statuses}
