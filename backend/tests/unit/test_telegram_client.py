from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from app.notifications.telegram import (
    TelegramClient,
    TelegramError,
    TelegramForbiddenError,
    TelegramRequestError,
    TelegramRetryAfterError,
    TelegramUnavailableError,
)
from tests.telegram_samples import API_BASE, TOKEN, error, method_url, ok, sent_messages


@pytest.fixture
async def telegram() -> AsyncIterator[TelegramClient]:
    async with httpx.AsyncClient() as http:
        yield TelegramClient(http, token=TOKEN, base_url=API_BASE, timeout_seconds=1)


async def test_send_message_posts_plain_text_without_link_preview(
    telegram: TelegramClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(method_url("sendMessage")).mock(return_value=ok({"message_id": 1}))

    await telegram.send_message(42, "Info gempa", reply_markup={"remove_keyboard": True})

    assert sent_messages(route) == [
        {
            "chat_id": 42,
            "text": "Info gempa",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"remove_keyboard": True},
        }
    ]


async def test_429_carries_retry_after(
    telegram: TelegramClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(method_url("sendMessage")).mock(
        return_value=error(429, "Too Many Requests: retry after 17", retry_after=17)
    )

    with pytest.raises(TelegramRetryAfterError) as caught:
        await telegram.send_message(42, "x")

    assert caught.value.retry_after == 17


async def test_429_without_retry_after_waits_at_least_a_second(
    telegram: TelegramClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(method_url("sendMessage")).mock(return_value=httpx.Response(429))

    with pytest.raises(TelegramRetryAfterError) as caught:
        await telegram.send_message(42, "x")

    assert caught.value.retry_after == 1


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (error(403, "Forbidden: bot was blocked by the user"), TelegramForbiddenError),
        (error(400, "Bad Request: chat not found"), TelegramRequestError),
        (error(500, "Internal Server Error"), TelegramUnavailableError),
        (error(502, "Bad Gateway"), TelegramUnavailableError),
        (httpx.Response(502, text="<html>gateway</html>"), TelegramUnavailableError),
        (httpx.Response(200, json={"ok": False, "description": "?"}), TelegramUnavailableError),
    ],
)
async def test_error_classification(
    telegram: TelegramClient,
    respx_mock: respx.MockRouter,
    response: httpx.Response,
    expected: type[TelegramError],
) -> None:
    respx_mock.post(method_url("sendMessage")).mock(return_value=response)

    with pytest.raises(expected):
        await telegram.send_message(42, "x")


@pytest.mark.parametrize(
    "failure", [httpx.ConnectTimeout("timed out"), httpx.ConnectError("connection refused")]
)
async def test_network_errors_are_retryable_and_never_leak_the_token(
    telegram: TelegramClient, respx_mock: respx.MockRouter, failure: Exception
) -> None:
    respx_mock.post(method_url("sendMessage")).mock(side_effect=failure)

    with pytest.raises(TelegramUnavailableError) as caught:
        await telegram.send_message(42, "x")

    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None  # the httpx error (whose request URL holds it) is dropped


async def test_error_descriptions_never_contain_the_token(
    telegram: TelegramClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(method_url("sendMessage")).mock(return_value=error(400, "Bad Request"))

    with pytest.raises(TelegramRequestError) as caught:
        await telegram.send_message(42, "x")

    assert TOKEN not in caught.value.description
    assert caught.value.description == "sendMessage: HTTP 400: Bad Request"


async def test_missing_token_fails_without_calling_telegram(
    respx_mock: respx.MockRouter,
) -> None:
    async with httpx.AsyncClient() as http:
        telegram = TelegramClient(http, token="", base_url=API_BASE, timeout_seconds=1)
        with pytest.raises(TelegramRequestError, match="not configured"):
            await telegram.send_message(42, "x")

    assert not respx_mock.calls


async def test_get_updates_passes_offset_and_long_poll_timeout(
    telegram: TelegramClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(method_url("getUpdates")).mock(return_value=ok([{"update_id": 7}]))

    updates = await telegram.get_updates(offset=7, timeout_seconds=30)

    assert updates == [{"update_id": 7}]
    assert sent_messages(route) == [{"timeout": 30, "allowed_updates": ["message"], "offset": 7}]
    assert route.calls[0].request.extensions["timeout"]["read"] == 40


async def test_get_me_returns_the_bot(
    telegram: TelegramClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(method_url("getMe")).mock(
        return_value=ok({"id": 1, "is_bot": True, "username": "quake_alert_dev_bot"})
    )

    assert (await telegram.get_me())["username"] == "quake_alert_dev_bot"
