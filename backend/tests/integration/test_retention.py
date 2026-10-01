from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import IngestionRun, IngestionStatus
from app.ingestion.domain import Feed
from app.ingestion.retention import prune_ingestion_runs
from app.ingestion.service import ingest_feed_payload
from tests.bmkg_samples import BASE_URL, load

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SETTINGS = Settings(
    bmkg_base_url=BASE_URL,
    ingestion_runs_retention_days=14,
    ingestion_runs_failed_retention_days=90,
)

SUCCESS, SKIPPED, FAILED = IngestionStatus.SUCCESS, IngestionStatus.SKIPPED, IngestionStatus.FAILED


def run(
    feed: Feed, status: IngestionStatus, days_ago: float, content_hash: str = "h"
) -> IngestionRun:
    return IngestionRun(
        feed=feed,
        status=status,
        fetched_at=NOW - timedelta(days=days_ago),
        content_hash=None if status is FAILED else content_hash,
    )


async def remaining(session: AsyncSession) -> set[tuple[Feed, IngestionStatus, float]]:
    rows = (await session.scalars(select(IngestionRun))).all()
    return {(r.feed, r.status, round((NOW - r.fetched_at) / timedelta(days=1), 3)) for r in rows}


async def prune(session: AsyncSession, *runs: IngestionRun) -> int:
    session.add_all(runs)
    await session.flush()
    return await prune_ingestion_runs(session, NOW, SETTINGS)


async def test_success_and_skipped_runs_expire_after_the_retention_window(
    db_session: AsyncSession,
) -> None:
    deleted = await prune(
        db_session,
        run(Feed.AUTOGEMPA, SUCCESS, 1),  # newest success: kept
        run(Feed.AUTOGEMPA, SUCCESS, 13.9),  # inside the window: kept
        run(Feed.AUTOGEMPA, SUCCESS, 14.1),
        run(Feed.AUTOGEMPA, SKIPPED, 13.9),
        run(Feed.AUTOGEMPA, SKIPPED, 20),
    )

    assert deleted == 2
    assert await remaining(db_session) == {
        (Feed.AUTOGEMPA, SUCCESS, 1),
        (Feed.AUTOGEMPA, SUCCESS, 13.9),
        (Feed.AUTOGEMPA, SKIPPED, 13.9),
    }


async def test_failed_runs_are_kept_for_90_days(db_session: AsyncSession) -> None:
    deleted = await prune(
        db_session,
        run(Feed.AUTOGEMPA, FAILED, 30),
        run(Feed.AUTOGEMPA, FAILED, 89.9),
        run(Feed.AUTOGEMPA, FAILED, 90.1),
    )

    assert deleted == 1
    assert await remaining(db_session) == {
        (Feed.AUTOGEMPA, FAILED, 30),
        (Feed.AUTOGEMPA, FAILED, 89.9),
    }


async def test_latest_successful_run_per_feed_is_never_deleted(db_session: AsyncSession) -> None:
    # autogempa hasn't changed for 40 days: its only success is old, followed by skips.
    # gempaterkini's latest success is just outside the window, with older ones before it.
    # gempadirasakan has never succeeded: nothing to protect.
    deleted = await prune(
        db_session,
        run(Feed.AUTOGEMPA, SUCCESS, 40),
        run(Feed.AUTOGEMPA, SKIPPED, 39),
        run(Feed.AUTOGEMPA, SKIPPED, 1),
        run(Feed.GEMPATERKINI, SUCCESS, 30),
        run(Feed.GEMPATERKINI, SUCCESS, 20),
        run(Feed.GEMPATERKINI, FAILED, 15),
        run(Feed.GEMPADIRASAKAN, FAILED, 100),
        run(Feed.GEMPADIRASAKAN, SKIPPED, 100),
    )

    assert deleted == 4
    assert await remaining(db_session) == {
        (Feed.AUTOGEMPA, SUCCESS, 40),  # guarded
        (Feed.AUTOGEMPA, SKIPPED, 1),
        (Feed.GEMPATERKINI, SUCCESS, 20),  # guarded
        (Feed.GEMPATERKINI, FAILED, 15),
    }


async def test_content_hash_skip_still_works_after_pruning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    payload = load(Feed.AUTOGEMPA)
    month_ago = NOW - timedelta(days=30)
    first = await ingest_feed_payload(session_factory, Feed.AUTOGEMPA, payload, month_ago, SETTINGS)
    assert first.status is SUCCESS

    async with session_factory() as session, session.begin():
        assert await prune_ingestion_runs(session, NOW, SETTINGS) == 0

    again = await ingest_feed_payload(session_factory, Feed.AUTOGEMPA, payload, NOW, SETTINGS)
    assert again.status is SKIPPED
