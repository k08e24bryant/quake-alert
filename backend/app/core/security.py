import logging
import secrets

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105  (a header name)


def verify_telegram_secret(request: Request) -> None:
    """FastAPI dependency for the Telegram webhook. Telegram sends back, in this header, the
    secret_token given to setWebhook; anything else is not Telegram. With no secret
    configured, every request is rejected (fail closed)."""
    expected = request.app.state.settings.telegram_webhook_secret.get_secret_value()
    given = request.headers.get(TELEGRAM_SECRET_HEADER, "")
    if not expected:
        logger.warning("Telegram webhook called but TELEGRAM_WEBHOOK_SECRET is not set")
    if not expected or not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or missing secret token.")
