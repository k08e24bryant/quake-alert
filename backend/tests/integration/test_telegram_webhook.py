from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import Float, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DeliveryStatus, NotificationDelivery, Subscription
from app.notifications import messages
from tests.integration.seed import seed_quake, seed_subscription
from tests.telegram_samples import CHAT_ID, WEBHOOK_SECRET, update

WEBHOOK = "/v1/telegram/webhook"
JAKARTA_PIN = (-6.208763, 106.845599)


async def post(
    api: AsyncClient, payload: Any, *, secret: str | None = WEBHOOK_SECRET
) -> httpx.Response:
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
    return await api.post(WEBHOOK, json=payload, headers=headers)


async def say(api: AsyncClient, **message: Any) -> dict[str, Any]:
    """Send one update; return the bot's reply (the sendMessage call in the response)."""
    response = await post(api, update(**message))
    assert response.status_code == 200
    reply: dict[str, Any] = response.json()
    assert reply["method"] == "sendMessage"
    assert reply["chat_id"] == message.get("chat_id", CHAT_ID)
    return reply


async def stored(session: AsyncSession, chat_id: int = CHAT_ID) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(
                func.ST_Y(func.geometry(Subscription.location), type_=Float).label("lat"),
                func.ST_X(func.geometry(Subscription.location), type_=Float).label("lon"),
                Subscription.radius_km,
                Subscription.min_magnitude,
                Subscription.is_active,
                Subscription.channel,
            ).where(Subscription.telegram_chat_id == chat_id)
        )
    ).first()
    return dict(row._mapping) if row else None


async def subscription_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Subscription)) or 0


# --- webhook authentication ---------------------------------------------------------------


@pytest.mark.parametrize("secret", [None, "", "wrong-secret", WEBHOOK_SECRET + "x"])
async def test_rejects_requests_without_the_right_secret(
    api: AsyncClient, db_session: AsyncSession, secret: str | None
) -> None:
    response = await post(api, update(location=JAKARTA_PIN), secret=secret)

    assert response.status_code == 403
    assert await subscription_count(db_session) == 0


async def test_rejects_everything_when_no_secret_is_configured(
    api: AsyncClient, api_app: FastAPI, db_session: AsyncSession
) -> None:
    settings = api_app.state.settings
    api_app.state.settings = settings.model_copy(update={"telegram_webhook_secret": SecretStr("")})

    for secret in ("", WEBHOOK_SECRET):
        response = await post(api, update(location=JAKARTA_PIN), secret=secret)
        assert response.status_code == 403
    assert await subscription_count(db_session) == 0


async def test_webhook_is_not_rate_limited(api: AsyncClient) -> None:
    response = await post(api, update(text="/list"))

    assert response.status_code == 200
    assert "X-RateLimit-Limit" not in response.headers


# --- /start and unknown input ---------------------------------------------------------------


async def test_start_explains_the_bot_with_disclaimer_and_location_button(
    api: AsyncClient,
) -> None:
    reply = await say(api, text="/start")

    assert reply["text"] == messages.START
    assert messages.DISCLAIMER in reply["text"]
    button = reply["reply_markup"]["keyboard"][0][0]
    assert button["request_location"] is True


async def test_unknown_text_gets_the_command_list(api: AsyncClient) -> None:
    for text in ("halo", "/nonsense"):
        reply = await say(api, text=text)
        assert reply["text"] == messages.UNKNOWN


async def test_ignores_group_chats_and_updates_without_a_message(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    group = update(location=JAKARTA_PIN, chat_type="group", chat_id=-100123)
    for payload in (group, {"update_id": 99}, {"update_id": 100, "edited_message": {}}):
        response = await post(api, payload)
        assert response.status_code == 200
        assert response.content == b""
    assert await subscription_count(db_session) == 0


async def test_malformed_update_is_acknowledged_not_retried(api: AsyncClient) -> None:
    # A 4xx/5xx would make Telegram redeliver the same broken update again and again.
    for payload in ({"nonsense": True}, {"update_id": "x", "message": 5}):
        response = await post(api, payload)
        assert response.status_code == 200
        assert response.content == b""


# --- location ------------------------------------------------------------------------------


async def test_location_creates_subscription_with_defaults_and_rounded_coordinates(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    reply = await say(api, location=JAKARTA_PIN)

    assert await stored(db_session) == {
        "lat": -6.21,
        "lon": 106.85,
        "radius_km": 200,
        "min_magnitude": Decimal("4.0"),
        "is_active": True,
        "channel": "telegram",
    }
    assert "-6.21, 106.85" in reply["text"]
    assert "-6.208763" not in reply["text"]
    assert reply["reply_markup"] == {"remove_keyboard": True}


async def test_new_location_moves_the_subscription_and_keeps_settings(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await say(api, location=JAKARTA_PIN)
    await say(api, text="/radius 300")
    await say(api, text="/minmag 5.5")

    await say(api, location=(-7.257472, 112.752088))  # Surabaya

    assert await subscription_count(db_session) == 1
    subscription = await stored(db_session)
    assert subscription is not None
    assert (subscription["lat"], subscription["lon"]) == (-7.26, 112.75)
    assert (subscription["radius_km"], subscription["min_magnitude"]) == (300, Decimal("5.5"))


# --- /radius and /minmag -------------------------------------------------------------------


async def test_radius_updates_the_subscription(api: AsyncClient, db_session: AsyncSession) -> None:
    await say(api, location=JAKARTA_PIN)

    reply = await say(api, text="/radius 50")

    assert reply["text"].startswith("Pengaturan diperbarui.")
    assert "Radius: 50 km" in reply["text"]
    assert (await stored(db_session) or {})["radius_km"] == 50


@pytest.mark.parametrize("argument", ["", "5", "1001", "abc", "12.5"])
async def test_invalid_radius_explains_the_format_and_changes_nothing(
    api: AsyncClient, db_session: AsyncSession, argument: str
) -> None:
    await say(api, location=JAKARTA_PIN)

    reply = await say(api, text=f"/radius {argument}")

    assert reply["text"] == messages.RADIUS_USAGE
    assert (await stored(db_session) or {})["radius_km"] == 200


async def test_min_magnitude_accepts_decimal_comma(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await say(api, location=JAKARTA_PIN)

    reply = await say(api, text="/minmag 4,5")

    assert "Magnitudo minimal: 4.5" in reply["text"]
    assert (await stored(db_session) or {})["min_magnitude"] == Decimal("4.5")


@pytest.mark.parametrize("argument", ["", "1.5", "9.5", "4.55", "lima"])
async def test_invalid_min_magnitude_explains_the_format_and_changes_nothing(
    api: AsyncClient, db_session: AsyncSession, argument: str
) -> None:
    await say(api, location=JAKARTA_PIN)

    reply = await say(api, text=f"/minmag {argument}")

    assert reply["text"] == messages.MIN_MAGNITUDE_USAGE
    assert (await stored(db_session) or {})["min_magnitude"] == Decimal("4.0")


@pytest.mark.parametrize("command", ["/radius 100", "/minmag 5", "/list"])
async def test_settings_commands_before_sharing_a_location(
    api: AsyncClient, db_session: AsyncSession, command: str
) -> None:
    reply = await say(api, text=command)

    assert reply["text"] == messages.NO_SUBSCRIPTION
    assert reply["reply_markup"]["keyboard"][0][0]["request_location"] is True
    assert await subscription_count(db_session) == 0


async def test_settings_command_reactivates_a_subscription_deactivated_by_a_block(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_subscription(db_session, chat_id=CHAT_ID, is_active=False)

    reply = await say(api, text="/radius 150")

    assert "Status: aktif" in reply["text"]
    assert (await stored(db_session) or {})["is_active"] is True


# --- /list ---------------------------------------------------------------------------------


async def test_list_shows_the_subscription(api: AsyncClient) -> None:
    await say(api, location=JAKARTA_PIN)

    reply = await say(api, text="/list")

    assert reply["text"].startswith("Langganan Anda:")
    for expected in ("-6.21, 106.85", "Radius: 200 km", "Magnitudo minimal: 4.0", "aktif"):
        assert expected in reply["text"]


# --- /stop ---------------------------------------------------------------------------------


async def test_stop_hard_deletes_the_subscription_and_its_deliveries(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await say(api, location=JAKARTA_PIN)
    mine = await db_session.scalar(
        select(Subscription.id).where(Subscription.telegram_chat_id == CHAT_ID)
    )
    other = await seed_subscription(db_session, chat_id=777)
    quake = await seed_quake(db_session)
    db_session.add_all(
        [
            NotificationDelivery(subscription_id=mine, earthquake_id=quake.id),
            NotificationDelivery(
                subscription_id=mine,
                earthquake_id=(await seed_quake(db_session)).id,
                status=DeliveryStatus.SENT,
            ),
            NotificationDelivery(subscription_id=other.id, earthquake_id=quake.id),
        ]
    )
    await db_session.flush()

    reply = await say(api, text="/stop")

    assert reply["text"] == messages.STOPPED
    assert await stored(db_session) is None
    remaining = (await db_session.scalars(select(NotificationDelivery.subscription_id))).all()
    assert remaining == [other.id]  # only the other chat's delivery is left
    assert await stored(db_session, chat_id=777) is not None


async def test_stop_without_subscription(api: AsyncClient) -> None:
    reply = await say(api, text="/stop")

    assert reply["text"] == messages.NOTHING_TO_STOP
