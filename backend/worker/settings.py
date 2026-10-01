from typing import ClassVar

from arq.connections import RedisSettings
from arq.cron import CronJob, cron
from arq.typing import WorkerCoroutine
from arq.worker import Function, func

from app.core.config import Settings, get_settings
from app.core.logging import logging_config
from worker.jobs import (
    deliver_notification,
    match_earthquakes,
    poll_bmkg_feeds,
    prune_old_ingestion_runs,
    shutdown,
    startup,
)

_settings: Settings = get_settings()

# arq's CLI applies its own plain-text logging; run it with
# `--custom-log-dict worker.settings.LOGGING_CONFIG` to get structured JSON logs instead.
LOGGING_CONFIG = logging_config(_settings.log_level)


class WorkerSettings:
    functions: ClassVar[list[Function | WorkerCoroutine]] = [
        # Both jobs raise arq Retry themselves (with backoff), so max_tries caps the attempts.
        func(match_earthquakes, max_tries=_settings.notify_max_attempts, timeout=60),
        func(deliver_notification, max_tries=_settings.notify_max_attempts, timeout=60),
    ]
    cron_jobs: ClassVar[list[CronJob]] = [
        cron(
            poll_bmkg_feeds,
            second=0,  # every minute, on the minute
            run_at_startup=True,
            unique=True,  # never overlap with a slow previous poll
            timeout=55,
            max_tries=1,  # the next minute is the retry
        ),
        cron(
            prune_old_ingestion_runs,
            hour=3,  # daily at 03:00 UTC
            minute=0,
            unique=True,
            timeout=300,
        ),
    ]
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
