"""arq job definitions. `ctx` is arq's per-worker dict; startup() fills it."""

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from arq import ArqRedis, Retry
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings, get_settings
from app.db.session import create_engine, create_sessionmaker
from app.earthquakes.cache import invalidate_latest
from app.ingestion.bmkg_client import BmkgClient, create_http_client
from app.ingestion.freshness import StalenessMonitor, read_freshness
from app.ingestion.retention import prune_ingestion_runs
from app.ingestion.service import FeedIngestionResult, ingest_all_feeds
from app.notifications.dispatcher import NotifyConfig, RetryDeliveryError, deliver
from app.notifications.matcher import create_deliveries, pending_delivery_ids
from app.notifications.telegram import TelegramClient

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    http = create_http_client(settings)
    ctx["settings"] = settings
    ctx["engine"] = engine
    ctx["session_factory"] = create_sessionmaker(engine)
    ctx["http"] = http
    ctx["bmkg_client"] = BmkgClient.from_settings(http, settings)
    ctx["telegram"] = TelegramClient.from_settings(http, settings)
    ctx["notify_config"] = NotifyConfig.from_settings(settings)
    ctx["staleness_monitor"] = StalenessMonitor()


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["http"].aclose()
    engine: AsyncEngine = ctx["engine"]
    await engine.dispose()


async def poll_bmkg_feeds(ctx: dict[str, Any]) -> dict[str, str]:
    """Fetch all BMKG feeds and ingest them (one ingestion_runs row per feed), then check
    freshness and re-enqueue any pending delivery that lost its job."""
    redis: ArqRedis = ctx["redis"]  # arq's own connection, to the same Redis the API uses

    async def on_change(result: FeedIngestionResult) -> None:
        await invalidate_latest(redis)
        if result.changed_earthquake_ids:
            await _enqueue_match(redis, result.changed_earthquake_ids)

    results = await ingest_all_feeds(
        ctx["session_factory"], ctx["bmkg_client"], ctx["settings"], on_change=on_change
    )
    await _check_freshness(ctx)
    await _requeue_pending_deliveries(ctx)
    return {result.feed.value: result.status.value for result in results}


async def match_earthquakes(ctx: dict[str, Any], earthquake_ids: list[str]) -> int:
    """Create deliveries for rows that were inserted or changed, from their current values
    in the database, and enqueue one send job per new delivery. Idempotent."""
    settings: Settings = ctx["settings"]
    try:
        async with ctx["session_factory"]() as session, session.begin():
            delivery_ids = await create_deliveries(
                session,
                [uuid.UUID(earthquake_id) for earthquake_id in earthquake_ids],
                now=datetime.now(UTC),
                max_age=timedelta(minutes=settings.notify_max_age_minutes),
            )
    except Exception as exc:
        _retry_or_raise(ctx, exc, "alert matching failed")
    for delivery_id in delivery_ids:
        await enqueue_delivery(ctx["redis"], delivery_id)
    if delivery_ids:
        logger.info(
            "alerts matched",
            extra={"earthquakes": len(earthquake_ids), "deliveries": len(delivery_ids)},
        )
    return len(delivery_ids)


async def deliver_notification(ctx: dict[str, Any], delivery_id: str) -> str:
    """Send one alert. Retries (arq Retry) with exponential backoff, or after Telegram's
    retry_after on a 429; see app.notifications.dispatcher."""
    try:
        outcome = await deliver(
            ctx["session_factory"],
            ctx["telegram"],
            uuid.UUID(delivery_id),
            attempt=ctx["job_try"],
            now=datetime.now(UTC),
            config=ctx["notify_config"],
        )
    except RetryDeliveryError as exc:
        raise Retry(defer=exc.defer_seconds) from exc
    except Exception as exc:
        _retry_or_raise(ctx, exc, "alert delivery crashed", delivery_id=delivery_id)
    return outcome.value


async def enqueue_delivery(redis: ArqRedis, delivery_id: uuid.UUID) -> None:
    # The job id makes this a no-op while the delivery's job is queued, running or waiting
    # to retry, so re-enqueueing a pending delivery can't double-send.
    await redis.enqueue_job(
        "deliver_notification", str(delivery_id), _job_id=f"delivery:{delivery_id}"
    )


async def _enqueue_match(redis: ArqRedis, earthquake_ids: Iterable[uuid.UUID]) -> None:
    ids = [str(earthquake_id) for earthquake_id in earthquake_ids]
    try:
        await redis.enqueue_job("match_earthquakes", ids)
    except Exception:
        # The rows are committed but won't be matched, so their alerts are missed. Only
        # possible if Redis fails between ingestion and enqueue; see README.
        logger.exception("could not enqueue alert matching", extra={"earthquake_ids": ids})


async def _check_freshness(ctx: dict[str, Any]) -> None:
    settings: Settings = ctx["settings"]
    monitor: StalenessMonitor = ctx["staleness_monitor"]
    try:
        async with ctx["session_factory"]() as session:
            freshness = await read_freshness(
                session,
                datetime.now(UTC),
                timedelta(minutes=settings.ingestion_stale_after_minutes),
            )
    except Exception:
        logger.exception("ingestion freshness check failed")
        return
    monitor.observe(freshness)


async def _requeue_pending_deliveries(ctx: dict[str, Any]) -> None:
    settings: Settings = ctx["settings"]
    try:
        async with ctx["session_factory"]() as session:
            pending = await pending_delivery_ids(
                session,
                now=datetime.now(UTC),
                max_age=timedelta(minutes=settings.notify_max_age_minutes),
            )
        for delivery_id in pending:
            await enqueue_delivery(ctx["redis"], delivery_id)
    except Exception:
        logger.exception("re-enqueueing pending deliveries failed")


def _retry_or_raise(ctx: dict[str, Any], exc: Exception, message: str, **extra: str) -> NoReturn:
    """Retry the job with backoff, or re-raise `exc` on its last try."""
    config: NotifyConfig = ctx["notify_config"]
    attempt: int = ctx["job_try"]
    if attempt >= config.max_attempts:
        raise exc
    logger.exception(message, extra={"attempt": attempt, **extra})
    raise Retry(defer=config.backoff_seconds(attempt)) from exc


async def prune_old_ingestion_runs(ctx: dict[str, Any]) -> int:
    """Daily retention for ingestion_runs; see app.ingestion.retention."""
    async with ctx["session_factory"]() as session, session.begin():
        deleted = await prune_ingestion_runs(session, datetime.now(UTC), ctx["settings"])
    logger.info("pruned ingestion_runs", extra={"deleted": deleted})
    return deleted
