"""The Telegram bot's conversation: a shared location or a command in, subscription changes
and one reply out.

Transport-agnostic: POST /v1/telegram/webhook returns the reply in its HTTP response (so the
API never needs the bot token), and the dev polling script sends it with sendMessage.
Only private chats are served; group and channel messages are ignored.
"""

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications import messages
from app.notifications.subscriptions import (
    MAX_MAGNITUDE,
    MAX_RADIUS_KM,
    MIN_MAGNITUDE,
    MIN_RADIUS_KM,
    SubscriptionView,
    delete_for_chat,
    get_for_chat,
    save_location,
    update_settings,
)
from app.schemas.telegram import Message, Update

logger = logging.getLogger(__name__)

_RADIUS = re.compile(r"\d{1,4}")
# "4", "4.5" and, as Indonesians often write it, "4,5". At most one decimal: the column is
# numeric(3,1) and silently rounding 4.55 would not be what the user asked for.
_MAGNITUDE = re.compile(r"\d(?:[.,]\d)?")


@dataclass(frozen=True, slots=True)
class BotReply:
    chat_id: int
    text: str
    reply_markup: dict[str, Any] | None = None

    def send_message_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": self.chat_id, "text": self.text}
        if self.reply_markup is not None:
            params["reply_markup"] = self.reply_markup
        return params


def parse_command(text: str) -> tuple[str, str] | None:
    """("radius", "150") for "/radius 150" or "/Radius@SomeBot  150"; None if not a command."""
    text = text.strip()
    if not text.startswith("/"):
        return None
    head, _, argument = text.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    return name, argument.strip()


def parse_radius(argument: str) -> int:
    if not _RADIUS.fullmatch(argument):
        raise ValueError(f"not a whole number of km: {argument!r}")
    radius = int(argument)
    if not MIN_RADIUS_KM <= radius <= MAX_RADIUS_KM:
        raise ValueError(f"radius out of range: {radius}")
    return radius


def parse_min_magnitude(argument: str) -> Decimal:
    if not _MAGNITUDE.fullmatch(argument):
        raise ValueError(f"not a magnitude with at most one decimal: {argument!r}")
    magnitude = Decimal(argument.replace(",", "."))
    if not MIN_MAGNITUDE <= magnitude <= MAX_MAGNITUDE:
        raise ValueError(f"magnitude out of range: {magnitude}")
    return magnitude.quantize(Decimal("0.1"))


async def handle_update(session: AsyncSession, payload: Any) -> BotReply | None:
    """Apply one Telegram update. None means nothing to reply (not a private message, or
    not an update we understand). Runs in its own transaction on `session`."""
    try:
        update = Update.model_validate(payload)
    except ValidationError as exc:
        logger.warning("ignoring malformed Telegram update", extra={"errors": exc.error_count()})
        return None
    message = update.message
    if message is None or message.chat.type != "private":
        return None
    async with session.begin():
        return await _reply_to(session, message)


async def _reply_to(session: AsyncSession, message: Message) -> BotReply:
    chat_id = message.chat.id

    def reply(text: str, markup: dict[str, Any] | None = None) -> BotReply:
        return BotReply(chat_id, text, markup)

    if message.location is not None:
        location = message.location
        saved = await save_location(session, chat_id, location.latitude, location.longitude)
        return reply(messages.location_saved(saved), messages.REMOVE_KEYBOARD)

    command = parse_command(message.text or "")
    if command is None:
        return reply(messages.UNKNOWN, messages.SHARE_LOCATION_KEYBOARD)
    name, argument = command

    if name in ("start", "help"):
        return reply(messages.START, messages.SHARE_LOCATION_KEYBOARD)
    view: SubscriptionView | None
    if name == "radius":
        try:
            radius_km = parse_radius(argument)
        except ValueError:
            return reply(messages.RADIUS_USAGE)
        view = await update_settings(session, chat_id, radius_km=radius_km)
    elif name == "minmag":
        try:
            min_magnitude = parse_min_magnitude(argument)
        except ValueError:
            return reply(messages.MIN_MAGNITUDE_USAGE)
        view = await update_settings(session, chat_id, min_magnitude=min_magnitude)
    elif name == "list":
        view = await get_for_chat(session, chat_id)
        if view is not None:
            return reply(messages.subscription_list(view))
    elif name == "stop":
        deleted = await delete_for_chat(session, chat_id)
        text = messages.STOPPED if deleted else messages.NOTHING_TO_STOP
        return reply(text, messages.REMOVE_KEYBOARD)
    else:
        return reply(messages.UNKNOWN, messages.SHARE_LOCATION_KEYBOARD)

    if view is None:
        return reply(messages.NO_SUBSCRIPTION, messages.SHARE_LOCATION_KEYBOARD)
    return reply(messages.settings_updated(view))
