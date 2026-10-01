"""How fresh the ingested data is. Read from ingestion_runs in Postgres only, never Redis, so
it stays truthful exactly when something is wrong.

A run counts as successful when its status is `success` or `skipped`: a skipped run fetched
the feed fine and found it unchanged, so the stored data is as current as BMKG's.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import CompoundSelect, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IngestionRun, IngestionStatus
from app.ingestion.domain import Feed
from app.ingestion.service import LATEST_RUN_FIRST

logger = logging.getLogger(__name__)

SUCCESSFUL_STATUSES = (IngestionStatus.SUCCESS, IngestionStatus.SKIPPED)


class IngestionState(StrEnum):
    OK = "ok"  # every feed had a successful run within the stale-after window
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class FeedFreshness:
    feed: Feed
    last_run_at: datetime | None
    last_run_status: IngestionStatus | None
    last_success_at: datetime | None

    def is_fresh(self, now: datetime, stale_after: timedelta) -> bool:
        return self.last_success_at is not None and now - self.last_success_at <= stale_after


@dataclass(frozen=True, slots=True)
class IngestionFreshness:
    feeds: list[FeedFreshness]
    checked_at: datetime
    stale_after: timedelta

    @property
    def state(self) -> IngestionState:
        if all(feed.is_fresh(self.checked_at, self.stale_after) for feed in self.feeds):
            return IngestionState.OK
        return IngestionState.STALE

    @property
    def stale_feeds(self) -> list[Feed]:
        return [f.feed for f in self.feeds if not f.is_fresh(self.checked_at, self.stale_after)]

    @property
    def data_as_of(self) -> datetime | None:
        return max((f.last_success_at for f in self.feeds if f.last_success_at), default=None)


def _newest_run_per_feed(*, successful_only: bool) -> CompoundSelect[Any]:
    """At most one row per feed. Each branch is a LIMIT 1 walk of
    ix_ingestion_runs_feed_fetched_at, so the cost doesn't grow with the table."""
    conditions = [IngestionRun.status.in_(SUCCESSFUL_STATUSES)] if successful_only else []
    return union_all(
        *(
            select(IngestionRun.feed, IngestionRun.status, IngestionRun.fetched_at)
            .where(IngestionRun.feed == feed, *conditions)
            .order_by(*LATEST_RUN_FIRST)
            .limit(1)
            for feed in Feed
        )
    )


async def read_freshness(
    session: AsyncSession, now: datetime, stale_after: timedelta
) -> IngestionFreshness:
    latest = (await session.execute(_newest_run_per_feed(successful_only=False))).all()
    successful = (await session.execute(_newest_run_per_feed(successful_only=True))).all()
    last_run = {feed: (status, fetched_at) for feed, status, fetched_at in latest}
    last_success = {feed: fetched_at for feed, _, fetched_at in successful}
    return IngestionFreshness(
        feeds=[
            FeedFreshness(
                feed=feed,
                last_run_at=last_run[feed][1] if feed in last_run else None,
                last_run_status=last_run[feed][0] if feed in last_run else None,
                last_success_at=last_success.get(feed),
            )
            for feed in Feed
        ],
        checked_at=now,
        stale_after=stale_after,
    )


async def read_data_as_of(session: AsyncSession) -> datetime | None:
    """The latest successful run across all feeds."""
    newest = _newest_run_per_feed(successful_only=True).subquery()
    as_of: datetime | None = await session.scalar(select(func.max(newest.c.fetched_at)))
    return as_of


class StalenessMonitor:
    """Logs ingestion going stale and recovering: once per transition, never per check.

    State lives in the process (the worker keeps one). A process that starts while
    ingestion is already stale logs it once; one that starts fresh logs nothing.
    """

    def __init__(self) -> None:
        self._state: IngestionState | None = None

    @property
    def state(self) -> IngestionState | None:
        return self._state

    def observe(self, freshness: IngestionFreshness) -> None:
        previous, self._state = self._state, freshness.state
        if freshness.state is previous:
            return
        if freshness.state is IngestionState.STALE:
            logger.warning(
                "BMKG ingestion is stale",
                extra={
                    "stale_feeds": [feed.value for feed in freshness.stale_feeds],
                    "stale_after_minutes": freshness.stale_after.total_seconds() / 60,
                    "data_as_of": freshness.data_as_of,
                },
            )
        elif previous is IngestionState.STALE:
            logger.info("BMKG ingestion recovered", extra={"data_as_of": freshness.data_as_of})
