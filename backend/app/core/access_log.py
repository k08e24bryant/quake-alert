"""Our own access log, replacing uvicorn's.

One structured line per HTTP request: method, path, status and duration. Nothing else:
- no query string, because it can hold a visitor's location ("Gempa di sekitar saya"
  sends lat/lon as query parameters) and, in general, anything a client puts there;
- no client IP and no headers.

scope["path"] never includes the query string. uvicorn's own access log (which logs the
full URL and the client address) is turned off: `--no-access-log` where uvicorn is started,
and silenced in app.core.logging in case it is started without it.
"""

import logging
import time
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("app.access")


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self.app = app
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = self.clock()
        # Stays 500 if the app raises before answering (the server then sends a 500).
        status = 500

        async def send_and_record_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_record_status)
        finally:
            logger.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((self.clock() - started) * 1000, 1),
                },
            )
