from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, status

from app.api.deps import SessionDep, SettingsDep
from app.core.rate_limit import enforce_rate_limit
from app.ingestion.freshness import read_freshness
from app.schemas.common import SourceAttribution
from app.schemas.status import FeedStatus, StatusResponse

router = APIRouter(
    prefix="/v1",
    tags=["status"],
    dependencies=[Depends(enforce_rate_limit)],
    responses={status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded"}},
)


@router.get("/status")
async def ingestion_status(session: SessionDep, settings: SettingsDep) -> StatusResponse:
    """How fresh the earthquake data is, per BMKG feed. Read from the database on every
    request (never cached). Not part of /readyz: stale data is still worth serving."""
    freshness = await read_freshness(
        session, datetime.now(UTC), timedelta(minutes=settings.ingestion_stale_after_minutes)
    )
    return StatusResponse(
        ingestion_state=freshness.state,
        stale_after_minutes=settings.ingestion_stale_after_minutes,
        checked_at=freshness.checked_at,
        feeds=[
            FeedStatus(
                feed=feed.feed.value,
                last_success_at=feed.last_success_at,
                last_run_status=feed.last_run_status,
                last_run_at=feed.last_run_at,
            )
            for feed in freshness.feeds
        ],
        source=SourceAttribution(data_as_of=freshness.data_as_of),
    )
