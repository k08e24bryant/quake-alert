from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import IngestionRun, IngestionStatus
from app.ingestion.service import LATEST_RUN_FIRST


async def prune_ingestion_runs(session: AsyncSession, now: datetime, settings: Settings) -> int:
    """Delete old ingestion_runs and return how many were deleted.

    success/skipped runs older than ingestion_runs_retention_days and failed runs older than
    ingestion_runs_failed_retention_days go, except the latest successful run of each feed:
    the content-hash skip compares against it, and a feed that hasn't changed for weeks
    has only `skipped` runs since.
    """
    latest_success_per_feed = (
        select(IngestionRun.id)
        .where(IngestionRun.status == IngestionStatus.SUCCESS)
        .ext(distinct_on(IngestionRun.feed))
        .order_by(IngestionRun.feed, *LATEST_RUN_FIRST)
    )
    normal_cutoff = now - timedelta(days=settings.ingestion_runs_retention_days)
    failed_cutoff = now - timedelta(days=settings.ingestion_runs_failed_retention_days)
    deleted = await session.scalars(
        delete(IngestionRun)
        .where(
            or_(
                and_(
                    IngestionRun.status.in_([IngestionStatus.SUCCESS, IngestionStatus.SKIPPED]),
                    IngestionRun.fetched_at < normal_cutoff,
                ),
                and_(
                    IngestionRun.status == IngestionStatus.FAILED,
                    IngestionRun.fetched_at < failed_cutoff,
                ),
            ),
            IngestionRun.id.not_in(latest_success_per_feed),
        )
        .returning(IngestionRun.id)
    )
    return len(deleted.all())
