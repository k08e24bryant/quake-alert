import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.config import Settings
from app.ingestion.domain import Feed

logger = logging.getLogger(__name__)

USER_AGENT = "quake-alert/0.1"


class BmkgError(Exception):
    """Fetching a BMKG feed failed."""


class BmkgUnavailableError(BmkgError):
    """BMKG kept failing with 5xx or network errors after all retries."""


class BmkgResponseError(BmkgError):
    """BMKG answered, but not with something we can use (4xx, non-JSON body). Not retried."""


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.bmkg_timeout_seconds,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


class BmkgClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        max_attempts: int,
        retry_backoff_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._sleep = sleep

    @classmethod
    def from_settings(cls, http: httpx.AsyncClient, settings: Settings) -> "BmkgClient":
        return cls(
            http,
            base_url=settings.bmkg_base_url,
            max_attempts=settings.bmkg_max_attempts,
            retry_backoff_seconds=settings.bmkg_retry_backoff_seconds,
        )

    def feed_url(self, feed: Feed) -> str:
        return f"{self._base_url}/{feed.value}.json"

    async def fetch(self, feed: Feed) -> Any:
        """Return the decoded JSON of a feed, retrying 5xx responses and network errors."""
        url = self.feed_url(feed)
        last_error = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._http.get(url)
            except httpx.TransportError as exc:  # connect/read errors and timeouts
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                elif response.is_error:
                    raise BmkgResponseError(f"{feed}: HTTP {response.status_code} from {url}")
                else:
                    return _decode_json(feed, response)

            if attempt < self._max_attempts:
                delay = self._retry_backoff_seconds * 2 ** (attempt - 1)
                logger.warning(
                    "BMKG fetch failed, retrying",
                    extra={
                        "feed": feed.value,
                        "attempt": attempt,
                        "retry_in_seconds": delay,
                        "error": last_error,
                    },
                )
                await self._sleep(delay)

        raise BmkgUnavailableError(
            f"{feed}: giving up after {self._max_attempts} attempts: {last_error}"
        )


def _decode_json(feed: Feed, response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # e.g. a Cloudflare HTML error page served with 200.
        content_type = response.headers.get("content-type", "unknown")
        raise BmkgResponseError(f"{feed}: response is not JSON ({content_type})") from exc
