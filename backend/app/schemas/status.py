from pydantic import BaseModel, Field

from app.db.models import IngestionStatus
from app.ingestion.freshness import IngestionState
from app.schemas.common import SourceAttribution, UtcDatetime


class FeedStatus(BaseModel):
    feed: str = Field(description="BMKG feed name, e.g. `autogempa`.")
    last_success_at: UtcDatetime | None = Field(
        description=(
            "Last run that read the feed successfully: status `success` (new content stored) "
            "or `skipped` (content unchanged). Null if it never succeeded."
        )
    )
    last_run_status: IngestionStatus | None = Field(
        description="Status of the most recent run, successful or not. Null if never run."
    )
    last_run_at: UtcDatetime | None


class StatusResponse(BaseModel):
    ingestion_state: IngestionState = Field(
        description=(
            "`ok` if every feed had a successful run within `stale_after_minutes`, otherwise "
            "`stale`. Stale data is still served: this is information, not an outage."
        )
    )
    stale_after_minutes: int
    checked_at: UtcDatetime
    feeds: list[FeedStatus]
    source: SourceAttribution
