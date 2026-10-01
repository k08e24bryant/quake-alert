from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from app.core.config import Settings
from app.ingestion.bmkg_client import (
    USER_AGENT,
    BmkgClient,
    BmkgResponseError,
    BmkgUnavailableError,
    create_http_client,
)
from app.ingestion.domain import Feed
from tests.bmkg_samples import BASE_URL, load

AUTOGEMPA_URL = f"{BASE_URL}autogempa.json"


@pytest.fixture
def sleeps() -> list[float]:
    """Backoff delays the client asked for (it is given a fake sleep that only records)."""
    return []


@pytest.fixture
async def client(sleeps: list[float]) -> AsyncIterator[BmkgClient]:
    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient() as http:
        yield BmkgClient(
            http, base_url=BASE_URL, max_attempts=3, retry_backoff_seconds=1.0, sleep=fake_sleep
        )


async def test_fetch_returns_decoded_json(client: BmkgClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(AUTOGEMPA_URL).respond(json=load(Feed.AUTOGEMPA))

    assert await client.fetch(Feed.AUTOGEMPA) == load(Feed.AUTOGEMPA)


@pytest.mark.parametrize("feed", list(Feed))
async def test_feed_urls(client: BmkgClient, feed: Feed) -> None:
    assert client.feed_url(feed) == f"https://data.bmkg.go.id/DataMKG/TEWS/{feed.value}.json"


async def test_5xx_is_retried_with_exponential_backoff(
    client: BmkgClient, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.get(AUTOGEMPA_URL).mock(
        side_effect=[
            httpx.Response(502),
            httpx.Response(503),
            httpx.Response(200, json=load(Feed.AUTOGEMPA)),
        ]
    )

    assert await client.fetch(Feed.AUTOGEMPA) == load(Feed.AUTOGEMPA)
    assert route.call_count == 3
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("read timed out"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
async def test_network_errors_are_retried(
    client: BmkgClient,
    respx_mock: respx.MockRouter,
    sleeps: list[float],
    error: httpx.TransportError,
) -> None:
    route = respx_mock.get(AUTOGEMPA_URL).mock(
        side_effect=[error, httpx.Response(200, json=load(Feed.AUTOGEMPA))]
    )

    assert await client.fetch(Feed.AUTOGEMPA) == load(Feed.AUTOGEMPA)
    assert route.call_count == 2


async def test_bmkg_down_gives_up_after_max_attempts(
    client: BmkgClient, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.get(AUTOGEMPA_URL).respond(503)

    with pytest.raises(BmkgUnavailableError, match=r"after 3 attempts: HTTP 503"):
        await client.fetch(Feed.AUTOGEMPA)
    assert route.call_count == 3
    assert sleeps == [1.0, 2.0]  # no pointless sleep after the last attempt


async def test_bmkg_unreachable_gives_up_after_max_attempts(
    client: BmkgClient, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    respx_mock.get(AUTOGEMPA_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(BmkgUnavailableError, match="ConnectTimeout"):
        await client.fetch(Feed.AUTOGEMPA)


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_4xx_is_not_retried(
    client: BmkgClient, respx_mock: respx.MockRouter, sleeps: list[float], status: int
) -> None:
    route = respx_mock.get(AUTOGEMPA_URL).respond(status)

    with pytest.raises(BmkgResponseError, match=f"HTTP {status}"):
        await client.fetch(Feed.AUTOGEMPA)
    assert route.call_count == 1
    assert sleeps == []


async def test_html_error_page_with_200_is_rejected(
    client: BmkgClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(AUTOGEMPA_URL).respond(
        200, html="<html><body>Attention Required! | Cloudflare</body></html>"
    )

    with pytest.raises(BmkgResponseError, match="not JSON"):
        await client.fetch(Feed.AUTOGEMPA)


async def test_http_client_uses_configured_timeout_and_user_agent() -> None:
    settings = Settings(bmkg_timeout_seconds=7.5)

    async with create_http_client(settings) as http:
        assert http.timeout == httpx.Timeout(7.5)
        assert http.headers["User-Agent"] == USER_AGENT


async def test_client_from_settings(respx_mock: respx.MockRouter) -> None:
    settings = Settings(bmkg_base_url="https://bmkg.example/TEWS", bmkg_max_attempts=1)
    route = respx_mock.get("https://bmkg.example/TEWS/gempaterkini.json").respond(503)

    async with httpx.AsyncClient() as http:
        client = BmkgClient.from_settings(http, settings)
        with pytest.raises(BmkgUnavailableError):
            await client.fetch(Feed.GEMPATERKINI)
    assert route.call_count == 1
