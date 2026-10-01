"""Builders for Telegram Bot API traffic in tests (Telegram itself is mocked with respx)."""

import itertools
import json
from typing import Any

import httpx
import respx

API_BASE = "https://telegram.test"
TOKEN = "123456:TEST-TOKEN-never-logged"  # noqa: S105  (fake)
CHAT_ID = 4242
WEBHOOK_SECRET = "test-webhook-secret"  # noqa: S105  (fake)

_update_ids = itertools.count(1)


def method_url(method: str) -> str:
    return f"{API_BASE}/bot{TOKEN}/{method}"


def update(
    *,
    text: str | None = None,
    location: tuple[float, float] | None = None,
    chat_id: int = CHAT_ID,
    chat_type: str = "private",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": next(_update_ids),
        "date": 1_790_000_000,
        "chat": {"id": chat_id, "type": chat_type, "first_name": "Test"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "Test"},
    }
    if text is not None:
        message["text"] = text
    if location is not None:
        message["location"] = {"latitude": location[0], "longitude": location[1]}
    return {"update_id": next(_update_ids), "message": message}


def ok(result: Any = True) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def error(status: int, description: str, **parameters: Any) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(status, json=body)


def sent_messages(route: respx.Route) -> list[dict[str, Any]]:
    """The JSON bodies of every call made to `route` (e.g. sendMessage)."""
    return [json.loads(call.request.content) for call in route.calls]
