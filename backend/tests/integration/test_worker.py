from arq import create_pool
from arq.connections import RedisSettings
from arq.worker import Worker
from redis.asyncio import Redis

from tests.integration.conftest import IntegrationSettings
from worker.settings import WorkerSettings


async def test_worker_settings_run_an_enqueued_job(
    integration_settings: IntegrationSettings, redis_client: Redis
) -> None:
    redis_settings = RedisSettings.from_dsn(integration_settings.test_redis_url)
    pool = await create_pool(redis_settings)
    # The worker shares our pool, so we close it ourselves. Worker.close() isn't used because
    # it signals SIGUSR1, which doesn't exist on Windows.
    worker = Worker(
        functions=WorkerSettings.functions,
        redis_pool=pool,
        burst=True,
        poll_delay=0.1,
        handle_signals=False,
    )
    try:
        job = await pool.enqueue_job("ping")
        assert job is not None

        await worker.main()

        assert await job.result(timeout=5) == "pong"
    finally:
        await pool.aclose()
