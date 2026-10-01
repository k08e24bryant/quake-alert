from typing import Any

import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Subscription
from scripts.telegram_polling import poll_once, run
from tests.telegram_samples import CHAT_ID, error, method_url, ok, sent_messages, update


async def test_poll_once_handles_updates_like_the_webhook_and_advances_the_offset(
    worker_ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    updates = [update(text="/start"), update(location=(-6.2088, 106.8456))]
    respx_mock.post(method_url("getUpdates")).mock(return_value=ok(updates))
    send = respx_mock.post(method_url("sendMessage")).mock(return_value=ok())

    offset = await poll_once(worker_ctx["telegram"], session_factory, None)

    assert offset == updates[-1]["update_id"] + 1
    replies = sent_messages(send)
    assert [r["chat_id"] for r in replies] == [CHAT_ID, CHAT_ID]
    assert replies[1]["text"].startswith("Lokasi tersimpan.")
    assert (
        await db_session.scalar(
            select(Subscription.radius_km).where(Subscription.telegram_chat_id == CHAT_ID)
        )
        == 200
    )


async def test_a_failed_reply_does_not_stop_the_batch(
    worker_ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    respx_mock: respx.MockRouter,
) -> None:
    updates = [update(text="/list", chat_id=1), update(text="/list", chat_id=2)]
    respx_mock.post(method_url("getUpdates")).mock(return_value=ok(updates))
    send = respx_mock.post(method_url("sendMessage"))
    send.side_effect = [error(403, "Forbidden: bot was blocked by the user"), ok()]

    offset = await poll_once(worker_ctx["telegram"], session_factory, 5)

    assert offset == updates[-1]["update_id"] + 1
    assert send.call_count == 2


async def test_empty_batch_keeps_the_offset(
    worker_ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(method_url("getUpdates")).mock(return_value=ok([]))

    assert await poll_once(worker_ctx["telegram"], session_factory, 41) == 41


async def test_refuses_to_run_in_production() -> None:
    settings = Settings(environment="production", telegram_bot_token=SecretStr("x:y"))

    with pytest.raises(SystemExit, match="dev only"):
        await run(settings)
