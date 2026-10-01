from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ingestion.bmkg_client import BmkgClient
from worker.jobs import poll_bmkg_feeds, shutdown, startup
from worker.settings import WorkerSettings


def test_bmkg_poll_is_a_unique_cron_job_every_minute() -> None:
    [job] = WorkerSettings.cron_jobs

    assert job.coroutine is poll_bmkg_feeds
    assert job.second == 0
    assert job.minute is None  # every minute
    assert job.unique is True
    assert job.run_at_startup is True
    assert job.timeout_s is not None and job.timeout_s < 60


async def test_startup_builds_and_shutdown_closes_the_job_context() -> None:
    ctx: dict[str, Any] = {}

    await startup(ctx)
    http: httpx.AsyncClient = ctx["http"]
    engine: AsyncEngine = ctx["engine"]
    assert isinstance(ctx["bmkg_client"], BmkgClient)
    assert not http.is_closed

    await shutdown(ctx)
    assert http.is_closed
    assert engine.pool.checkedout() == 0
