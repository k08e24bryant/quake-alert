"""arq job definitions. `ctx` is arq's per-worker dict; startup() fills it."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from arq import ArqRedis, Retry
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings, get_settings
from app.core.crypto import check_startup_secrets
from app.db.session import create_engine, create_sessionmaker
from app.earthquakes.cache import invalidate_latest
from app.ingestion.bmkg_client import BmkgClient, create_http_client
from app.ingestion.freshness import StalenessMonitor, read_freshness
from app.ingestion.retention import prune_ingestion_runs
from app.ingestion.service import FeedIngestionResult, ingest_all_feeds
from app.notifications.dispatcher import NotifyConfig, RetryDeliveryError, deliver
from app.notifications.matcher import MatchResult, match_flagged, pending_delivery_ids
from app.notifications.retention import prune_notification_deliveries
from app.notifications.telegram import TelegramClient
from app.notifications.webhook import WebhookNotifier, create_webhook_http_client

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    check_startup_secrets(settings)  # e.g. no WEBHOOK_SECRET_KEYS in production
    engine = create_engine(settings)
    http = create_http_client(settings)
    ctx["settings"] = settings
    ctx["engine"] = engine
    ctx["session_factory"] = create_sessionmaker(engine)
    ctx["http"] = http
    ctx["bmkg_client"] = BmkgClient.from_settings(http, settings)
    ctx["telegram"] = TelegramClient.from_settings(http, settings)
    ctx["webhook_http"] = create_webhook_http_client()
    ctx["webhook"] = WebhookNotifier.from_settings(ctx["webhook_http"], settings)
    ctx["notify_config"] = NotifyConfig.from_settings(settings)
    ctx["staleness_monitor"] = StalenessMonitor()


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["http"].aclose()
    await ctx["webhook_http"].aclose()
    engine: AsyncEngine = ctx["engine"]
    await engine.dispose()


async def poll_bmkg_feeds(ctx: dict[str, Any]) -> dict[str, str]:
    """Sweep what earlier runs left behind, then fetch all BMKG feeds and ingest them (one
    ingestion_runs row per feed), then check freshness.

    The sweep matches rows still flagged needs_matching (their match job was lost, e.g. the
    enqueue failed after the feed commit) and re-enqueues pending deliveries whose send job
    was lost. It runs first, so it only ever picks up what an earlier poll left.
    """
    redis: ArqRedis = ctx["redis"]  # arq's own connection, to the same Redis the API uses
    await _sweep(ctx)

    async def on_change(result: FeedIngestionResult) -> None:
        await invalidate_latest(redis)
        if result.changed_earthquake_ids:
            await _enqueue_match(redis)

    results = await ingest_all_feeds(
        ctx["session_factory"], ctx["bmkg_client"], ctx["settings"], on_change=on_change
    )
    await _check_freshness(ctx)
    return {result.feed.value: result.status.value for result in results}


async def match_earthquakes(ctx: dict[str, Any]) -> int:
    """Drain the needs_matching outbox: create deliveries for flagged rows from their current
    values in the database, clear the flags in the same transaction, then enqueue one send
    job per new delivery. Idempotent; concurrent runs split the rows (SKIP LOCKED)."""
    try:
        result = await _match_flagged(ctx)
    except Exception as exc:
        _retry_or_raise(ctx, exc, "alert matching failed")
    return len(result.delivery_ids)


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
            webhook=ctx.get("webhook"),
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


async def _enqueue_match(redis: ArqRedis) -> None:
    try:
        await redis.enqueue_job("match_earthquakes")
    except Exception:
        # Nothing is lost: the rows stay flagged needs_matching, and the next poll's sweep
        # matches them.
        logger.exception("could not enqueue alert matching; the next poll will sweep it")


async def _match_flagged(ctx: dict[str, Any]) -> MatchResult:
    settings: Settings = ctx["settings"]
    async with ctx["session_factory"]() as session, session.begin():
        result = await match_flagged(
            session,
            now=datetime.now(UTC),
            max_age=timedelta(minutes=settings.notify_max_age_minutes),
        )
    # After the commit. If an enqueue fails, the delivery stays pending and the next poll's
    # sweep re-enqueues it.
    for delivery_id in result.delivery_ids:
        await enqueue_delivery(ctx["redis"], delivery_id)
    if result.delivery_ids:
        logger.info(
            "alerts matched",
            extra={"earthquakes": len(result.matched), "deliveries": len(result.delivery_ids)},
        )
    return result


async def _sweep(ctx: dict[str, Any]) -> None:
    try:
        await _match_flagged(ctx)
    except Exception:
        logger.exception("sweep of flagged earthquakes failed")
    await _requeue_pending_deliveries(ctx)


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


async def prune_old_records(ctx: dict[str, Any]) -> dict[str, int]:
    """Daily retention for ingestion_runs and notification_deliveries; see
    app.ingestion.retention and app.notifications.retention."""
    now = datetime.now(UTC)
    async with ctx["session_factory"]() as session, session.begin():
        runs = await prune_ingestion_runs(session, now, ctx["settings"])
        deliveries = await prune_notification_deliveries(session, now, ctx["settings"])
    deleted = {"ingestion_runs": runs, "notification_deliveries": deliveries}
    logger.info("pruned old records", extra=deleted)
    return deleted
