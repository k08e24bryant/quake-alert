import uuid
from datetime import UTC, datetime, timedelta
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.earthquakes.pagination import Cursor, InvalidCursorError
from app.schemas.common import SourceAttribution, UtcDatetime

MAX_RADIUS_KM = 1000
MAX_RANGE = timedelta(days=366)
MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class Earthquake(BaseModel):
    id: uuid.UUID
    occurred_at: UtcDatetime = Field(description="Origin time, UTC.")
    magnitude: float
    depth_km: int
    latitude: float
    longitude: float
    region: str = Field(description="BMKG's free-text region description (Wilayah).")
    potential: str | None = Field(
        description=(
            'BMKG\'s "Potensi" free text, verbatim. This is NOT tsunami information: it is '
            "often a tsunami statement, but can be any message (e.g. that the quake was felt). "
            "Never present it as a tsunami status."
        )
    )
    felt: str | None = Field(description="BMKG's felt report (Dirasakan), MMI scale text.")
    shakemap_url: str | None
    source_feeds: list[str] = Field(
        description="BMKG feeds this quake was seen in, highest precedence first."
    )
    distance_km: float | None = Field(
        default=None,
        description="Distance from the lat/lon query point. Null unless lat and lon are given.",
    )


class EarthquakeList(BaseModel):
    data: list[Earthquake]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page. Null on the last page."
    )
    source: SourceAttribution


class EarthquakeDetail(BaseModel):
    data: Earthquake
    source: SourceAttribution


class EarthquakeQuery(BaseModel):
    """Query parameters of GET /v1/earthquakes."""

    model_config = ConfigDict(extra="forbid")

    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float | None = Field(
        default=None, gt=0, le=MAX_RADIUS_KM, description="Requires lat and lon."
    )
    min_mag: float | None = Field(default=None, ge=0, lt=10)
    max_mag: float | None = Field(default=None, ge=0, lt=10)
    start: AwareDatetime | None = Field(
        default=None, description="Inclusive. ISO 8601 with offset, e.g. 2026-10-01T00:00:00Z."
    )
    end: AwareDatetime | None = Field(default=None, description="Exclusive.")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    cursor: str | None = None

    @field_validator("start", "end")
    @classmethod
    def _to_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value else None

    @field_validator("cursor")
    @classmethod
    def _valid_cursor(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                Cursor.decode(value)
            except InvalidCursorError as exc:
                raise ValueError("cursor is invalid; use next_cursor from a previous page") from exc
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must be given together")
        if self.radius_km is not None and self.lat is None:
            raise ValueError("radius_km requires lat and lon")
        if self.min_mag is not None and self.max_mag is not None and self.min_mag > self.max_mag:
            raise ValueError("min_mag must not be greater than max_mag")
        if self.start is not None:
            end = self.end or datetime.now(UTC)
            if end <= self.start:
                raise ValueError("end must be after start")
            if end - self.start > MAX_RANGE:
                raise ValueError("start..end may span at most 366 days (end defaults to now)")
        return self

    @property
    def decoded_cursor(self) -> Cursor | None:
        return Cursor.decode(self.cursor) if self.cursor else None
