from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, Field
from pydantic.functional_serializers import PlainSerializer

# Always "+00:00", never "Z": the API contract is ISO 8601 with an explicit offset.
UtcDatetime = Annotated[
    datetime, PlainSerializer(lambda dt: dt.astimezone(UTC).isoformat(), return_type=str)
]


class SourceAttribution(BaseModel):
    """BMKG requires attribution wherever its data is shown."""

    name: str = "BMKG (Badan Meteorologi, Klimatologi, dan Geofisika)"
    url: str = "https://data.bmkg.go.id/"
    notice: str = "Earthquake data from BMKG Open Data."
    data_as_of: UtcDatetime | None = Field(
        default=None,
        description=(
            "When BMKG was last read successfully (any feed), as of when this response was "
            "produced; cached responses keep the value from when they were cached. Null if "
            "no feed has ever been read. See GET /v1/status for per-feed detail."
        ),
    )
