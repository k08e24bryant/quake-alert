"""Dev only: receive Telegram updates by long polling (getUpdates) instead of the webhook,
so the bot works locally without a public HTTPS URL.

    cd backend
    uv run python -m scripts.telegram_polling

Reads backend/.env (DATABASE_URL, TELEGRAM_BOT_TOKEN). Telegram refuses getUpdates while a
webhook is set, so delete it first (see README). Refuses to run with ENVIRONMENT=production,
where updates arrive at POST /v1/telegram/webhook.
"""

import asyncio
import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_sessionmaker
from app.notifications.bot import handle_update
from app.notifications.telegram import (
    TelegramClient,
    TelegramError,
    TelegramRequestError,
    TelegramUnavailableError,
)

logger = logging.getLogger("scripts.telegram_polling")

POLL_TIMEOUT_SECONDS = 30
ERROR_PAUSE_SECONDS = 5


async def poll_once(
    telegram: TelegramClient,
    session_factory: async_sessionmaker[AsyncSession],
    offset: int | None,
) -> int | None:
    """Fetch one batch of updates, handle each like the webhook does, send the replies.
    Returns the next offset, which also tells Telegram the batch was received."""
    updates = await telegram.get_updates(offset=offset, timeout_seconds=POLL_TIMEOUT_SECONDS)
    for update in updates:
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if isinstance(update_id, int):
            offset = update_id + 1
        async with session_factory() as session:
            reply = await handle_update(session, update)
        if reply is None:
            continue
        try:
            await telegram.send_message(reply.chat_id, reply.text, reply_markup=reply.reply_markup)
        except TelegramError as exc:
            logger.warning("could not send bot reply", extra={"error": exc.description})
    return offset


async def run(settings: Settings) -> None:
    if settings.environment == "production":
        raise SystemExit("dev only: production receives updates at POST /v1/telegram/webhook")
    if not settings.telegram_bot_token.get_secret_value():
        raise SystemExit("set TELEGRAM_BOT_TOKEN in backend/.env")
    engine = create_engine(settings)
    session_factory = create_sessionmaker(engine)
    offset: int | None = None
    try:
        async with httpx.AsyncClient() as http:
            telegram = TelegramClient.from_settings(http, settings)
            logger.info("polling Telegram for updates (Ctrl+C to stop)")
            while True:
                try:
                    offset = await poll_once(telegram, session_factory, offset)
                except TelegramUnavailableError as exc:
                    logger.warning("getUpdates failed", extra={"error": exc.description})
                    await asyncio.sleep(ERROR_PAUSE_SECONDS)
                except TelegramRequestError as exc:
                    if exc.status_code == 409:
                        raise SystemExit(
                            "a webhook is set for this bot; delete it first (see README)"
                        ) from None
                    raise
    finally:
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
