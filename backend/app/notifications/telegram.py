"""Telegram Bot API over plain httpx (no SDK).

The bot token is part of every request URL, so nothing here ever puts a URL or an httpx
exception message into an error: those would leak the token into logs and last_error.
Errors carry the method name, the HTTP status and Telegram's own description only.
"""

from typing import Any

import httpx

from app.core.config import Settings


class TelegramError(Exception):
    def __init__(self, description: str, *, status_code: int | None = None) -> None:
        super().__init__(description)
        self.description = description
        self.status_code = status_code


class TelegramUnavailableError(TelegramError):
    """Network error, timeout or 5xx: worth retrying."""


class TelegramRetryAfterError(TelegramError):
    """429 flood control: retry, but not before `retry_after` seconds."""

    def __init__(self, description: str, *, retry_after: int) -> None:
        super().__init__(description, status_code=429)
        self.retry_after = retry_after


class TelegramForbiddenError(TelegramError):
    """403: the user blocked the bot (or deleted their account). Never retry."""


class TelegramRequestError(TelegramError):
    """Any other 4xx (e.g. chat not found, bad request): retrying won't help."""


class TelegramClient:
    def __init__(
        self, http: httpx.AsyncClient, *, token: str, base_url: str, timeout_seconds: float
    ) -> None:
        self._http = http
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    @classmethod
    def from_settings(cls, http: httpx.AsyncClient, settings: Settings) -> "TelegramClient":
        return cls(
            http,
            token=settings.telegram_bot_token.get_secret_value(),
            base_url=settings.telegram_api_base_url,
            timeout_seconds=settings.telegram_timeout_seconds,
        )

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> None:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        await self.call("sendMessage", params)

    async def get_me(self) -> dict[str, Any]:
        """The bot this token belongs to (id, username, ...)."""
        result = await self.call("getMe", {})
        return result if isinstance(result, dict) else {}

    async def get_updates(self, *, offset: int | None, timeout_seconds: int) -> list[Any]:
        """Long polling. Fails with 409 while a webhook is set (see deleteWebhook)."""
        params: dict[str, Any] = {"timeout": timeout_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        result = await self.call("getUpdates", params, timeout_seconds=timeout_seconds + 10)
        return list(result or [])

    async def call(
        self, method: str, params: dict[str, Any], *, timeout_seconds: float | None = None
    ) -> Any:
        if not self._token:
            raise TelegramRequestError(f"{method}: TELEGRAM_BOT_TOKEN is not configured")
        try:
            response = await self._http.post(
                f"{self._base_url}/bot{self._token}/{method}",
                json=params,
                timeout=timeout_seconds or self._timeout,
            )
        except httpx.TimeoutException:
            raise TelegramUnavailableError(f"{method}: timeout") from None
        except httpx.TransportError as exc:
            raise TelegramUnavailableError(f"{method}: {type(exc).__name__}") from None

        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        status = response.status_code
        if status == 200 and body.get("ok") is True:
            return body.get("result")

        description = f"{method}: HTTP {status}: {str(body.get('description', ''))[:200]}"
        if status == 429:
            parameters = body.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            seconds = retry_after if isinstance(retry_after, int) and retry_after > 0 else 1
            raise TelegramRetryAfterError(description, retry_after=seconds)
        if status == 403:
            raise TelegramForbiddenError(description, status_code=status)
        if status >= 500 or status == 200:  # 200 with ok=false or a non-JSON body
            raise TelegramUnavailableError(description, status_code=status)
        raise TelegramRequestError(description, status_code=status)
