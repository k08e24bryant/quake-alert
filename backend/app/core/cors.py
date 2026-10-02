"""CORS for the public read API, and nothing else.

Browsers on CORS_ALLOWED_ORIGINS (the frontend) may GET /v1/earthquakes* and /v1/status.
Every other path (subscriptions, the Telegram webhook, health) gets no CORS headers at
all, so a browser on another origin can't read their responses. Starlette's
CORSMiddleware alone applies to every path; this wraps it so it only sees the paths above.

Origins are exact (scheme://host[:port], no path). A wildcard is refused in production:
the API and the worker share settings, and an explicit list is the only safe default.
"""

from collections.abc import Sequence
from urllib.parse import urlsplit

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings

WILDCARD = "*"
# Exact paths, or a prefix followed by "/". "/v1/earthquakesX" is not covered.
CORS_PATHS = ("/v1/earthquakes", "/v1/status")


def cors_origins(settings: Settings) -> list[str]:
    """The configured origins, validated. Raises ValueError for a malformed origin, or a
    wildcard in production."""
    origins = [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
    for origin in origins:
        if origin == WILDCARD:
            if settings.environment == "production":
                raise ValueError("CORS_ALLOWED_ORIGINS must not contain '*' in production")
            continue
        parts = urlsplit(origin)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or origin != f"{parts.scheme}://{parts.netloc}"  # no path, query, fragment or "/"
            or parts.username is not None
        ):
            raise ValueError(
                f"CORS_ALLOWED_ORIGINS entry {origin!r} is not an origin like "
                "https://example.com or http://localhost:3000"
            )
    return origins


def covered(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in CORS_PATHS)


class ReadApiCORSMiddleware:
    """CORSMiddleware (GET only, no credentials) for CORS_PATHS; a pass-through elsewhere."""

    def __init__(self, app: ASGIApp, *, allow_origins: Sequence[str]) -> None:
        self.app = app
        self.cors = CORSMiddleware(
            app, allow_origins=allow_origins, allow_methods=["GET"], allow_credentials=False
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and covered(scope["path"]):
            await self.cors(scope, receive, send)
        else:
            await self.app(scope, receive, send)
