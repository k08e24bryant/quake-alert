from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.typing import WorkerCoroutine

from app.core.config import get_settings
from app.core.logging import logging_config

_settings = get_settings()

# arq's CLI applies its own plain-text logging; run it with
# `--custom-log-dict worker.settings.LOGGING_CONFIG` to get structured JSON logs instead.
LOGGING_CONFIG = logging_config(_settings.log_level)


async def ping(ctx: dict[str, Any]) -> str:
    """No-op job: arq refuses to start with zero registered functions.

    Remove once the first real job is registered.
    """
    return "pong"


class WorkerSettings:
    functions: ClassVar[list[WorkerCoroutine]] = [ping]
    cron_jobs: ClassVar[list[CronJob]] = []
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
