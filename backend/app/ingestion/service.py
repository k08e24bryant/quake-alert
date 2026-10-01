import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import IngestionRun, IngestionStatus
from app.ingestion.bmkg_client import BmkgClient
from app.ingestion.dedup import DedupConfig, UpsertOutcome, upsert_report
from app.ingestion.domain import Feed
from app.ingestion.parser import parse_feed

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FeedIngestionResult:
    feed: Feed
    status: IngestionStatus
    inserted_count: int = 0
    updated_count: int = 0
    # Items not stored: malformed ones the parser dropped, plus items whose matching row
    # has a stored payload that no longer parses (that row is left unchanged).
    skipped_count: int = 0
    error: str | None = None
    # Rows inserted, or updated in a derived column (not just `raw`): the rows whose alert
    # matching may have changed.
    changed_earthquake_ids: tuple[uuid.UUID, ...] = ()


# How "the latest run" is chosen everywhere (content-hash skip, retention guard). The id
# tiebreak keeps the choice deterministic if two runs share a fetched_at.
LATEST_RUN_FIRST = (IngestionRun.fetched_at.desc(), IngestionRun.id.desc())


def content_hash(payload: Any) -> str:
    """sha256 of the payload as canonical JSON, so whitespace or key order don't count."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


OnChange = Callable[[FeedIngestionResult], Awaitable[None]]


async def ingest_all_feeds(
    session_factory: async_sessionmaker[AsyncSession],
    client: BmkgClient,
    settings: Settings,
    on_change: OnChange | None = None,
) -> list[FeedIngestionResult]:
    """Fetch every feed concurrently, then process them one at a time.

    Processing is sequential on purpose: the same quake appears in several feeds, and
    concurrent upserts of near-duplicates (different fingerprints) could both insert.
    `on_change` runs with the feed's result after each feed commit that inserted or updated
    rows (e.g. to invalidate the API's `latest` cache and enqueue alert matching).
    """
    feeds = list(Feed)
    fetched_at = datetime.now(UTC)
    payloads = await asyncio.gather(*(client.fetch(feed) for feed in feeds), return_exceptions=True)
    results = []
    for feed, payload in zip(feeds, payloads, strict=True):
        if isinstance(payload, BaseException) and not isinstance(payload, Exception):
            raise payload  # cancellation and friends must propagate
        result = await ingest_feed_payload(session_factory, feed, payload, fetched_at, settings)
        if on_change is not None and (result.inserted_count or result.updated_count):
            await on_change(result)
        results.append(result)
    return results


async def ingest_feed_payload(
    session_factory: async_sessionmaker[AsyncSession],
    feed: Feed,
    payload: Any,
    fetched_at: datetime,
    settings: Settings,
) -> FeedIngestionResult:
    """Store one feed's payload (or the exception raised fetching it) and record the run.

    The quakes and the run row commit together; on any failure both roll back and a
    `failed` run is recorded in a fresh transaction.
    """
    if isinstance(payload, Exception):
        return await _record_failure(session_factory, feed, fetched_at, None, payload)

    digest = content_hash(payload)
    try:
        async with session_factory() as session, session.begin():
            if digest == await _last_successful_hash(session, feed):
                result = FeedIngestionResult(feed, IngestionStatus.SKIPPED)
            else:
                result = await _store_reports(session, feed, payload, settings)
            session.add(_run_row(result, fetched_at, digest))
    except Exception as exc:
        logger.exception("BMKG feed ingestion failed", extra={"feed": feed.value})
        return await _record_failure(session_factory, feed, fetched_at, digest, exc)

    logger.info(
        "BMKG feed ingested",
        extra={
            "feed": feed.value,
            "status": result.status.value,
            "inserted": result.inserted_count,
            "updated": result.updated_count,
            "skipped_items": result.skipped_count,
        },
    )
    return result


async def _store_reports(
    session: AsyncSession, feed: Feed, payload: Any, settings: Settings
) -> FeedIngestionResult:
    config = DedupConfig.from_settings(settings)
    parsed = parse_feed(feed, payload, settings.bmkg_base_url)
    # Items of one snapshot are distinct quakes: none may resolve to a row another claimed.
    claimed: set[uuid.UUID] = set()
    results = [await upsert_report(session, report, config, claimed) for report in parsed.reports]
    outcomes = [result.outcome for result in results]
    return FeedIngestionResult(
        feed,
        IngestionStatus.SUCCESS,
        inserted_count=outcomes.count(UpsertOutcome.INSERTED),
        updated_count=outcomes.count(UpsertOutcome.UPDATED),
        skipped_count=parsed.skipped_count + outcomes.count(UpsertOutcome.SKIPPED),
        changed_earthquake_ids=tuple(r.earthquake_id for r in results if r.fields_changed),
    )


async def _last_successful_hash(session: AsyncSession, feed: Feed) -> str | None:
    return await session.scalar(
        select(IngestionRun.content_hash)
        .where(IngestionRun.feed == feed, IngestionRun.status == IngestionStatus.SUCCESS)
        .order_by(*LATEST_RUN_FIRST)
        .limit(1)
    )


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    feed: Feed,
    fetched_at: datetime,
    digest: str | None,
    exc: Exception,
) -> FeedIngestionResult:
    result = FeedIngestionResult(feed, IngestionStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
    logger.warning(
        "BMKG feed ingestion recorded as failed",
        extra={"feed": feed.value, "error": result.error},
    )
    async with session_factory() as session, session.begin():
        session.add(_run_row(result, fetched_at, digest))
    return result


def _run_row(result: FeedIngestionResult, fetched_at: datetime, digest: str | None) -> IngestionRun:
    return IngestionRun(
        feed=result.feed,
        fetched_at=fetched_at,
        status=result.status,
        content_hash=digest,
        inserted_count=result.inserted_count,
        updated_count=result.updated_count,
        skipped_count=result.skipped_count,
        error=result.error,
    )
