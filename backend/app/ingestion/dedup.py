"""Store QuakeReports so that each real quake is exactly one earthquakes row.

BMKG has no quake ID, and the same quake shows up in several feeds with slightly different
fields. A report matches an existing row when either:
  1. its fingerprint (UTC time to the second + lat/lon rounded to 2 decimals) is equal, or
  2. the nearest row in time is within dedup_max_time_diff_seconds AND
     dedup_max_distance_km of it (catches revised times/positions).

Merge rules (deterministic: the row depends only on the current set of feed payloads,
never on the order they were polled in):
  - `raw` holds the latest payload item per feed, keyed by feed name. A report replaces
    its own feed's entry and nothing else.
  - Every other column is re-derived from `raw` on every merge, by FEED_PRECEDENCE
    (autogempa > gempaterkini > gempadirasakan):
      * occurred_at, magnitude, location, depth_km, region: from the highest-precedence
        feed present;
      * felt, potential, shakemap_url: from the highest-precedence feed that has a value;
      * source_feeds: the feeds present, in precedence order.
  - fingerprint is set by the first report and never changes, so the unique key never moves.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from geoalchemy2 import WKTElement
from sqlalchemy import Float, Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Earthquake
from app.ingestion.domain import FEED_PRECEDENCE, QuakeReport
from app.ingestion.parser import parse_item

_LATITUDE = func.ST_Y(func.geometry(Earthquake.location), type_=Float)
_LONGITUDE = func.ST_X(func.geometry(Earthquake.location), type_=Float)


class UpsertOutcome(StrEnum):
    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class DedupConfig:
    max_time_diff: timedelta
    max_distance_m: float
    shakemap_base_url: str  # needed to re-derive shakemap_url from raw payloads

    @classmethod
    def from_settings(cls, settings: Settings) -> "DedupConfig":
        return cls(
            max_time_diff=timedelta(seconds=settings.dedup_max_time_diff_seconds),
            max_distance_m=settings.dedup_max_distance_km * 1000,
            shakemap_base_url=settings.bmkg_base_url,
        )


@dataclass(frozen=True, slots=True)
class DerivedQuake:
    """The columns of an earthquakes row that are computed from its raw payloads."""

    columns: dict[str, Any]
    latitude: Decimal
    longitude: Decimal

    @property
    def location(self) -> WKTElement:
        return WKTElement(f"POINT({self.longitude} {self.latitude})", srid=4326)


@dataclass(slots=True)
class _Match:
    row: Earthquake
    latitude: Decimal
    longitude: Decimal


def derive(raw: Mapping[str, Any], shakemap_base_url: str) -> DerivedQuake:
    """Compute a row's columns from its per-feed payloads by FEED_PRECEDENCE."""
    reports = [
        parse_item(feed, raw[feed.value], shakemap_base_url)
        for feed in FEED_PRECEDENCE
        if feed.value in raw
    ]
    if not reports:
        raise ValueError(f"no known feed in raw payloads: {sorted(raw)}")
    primary = reports[0]

    def first_present(field: str) -> Any:
        return next((v for r in reports if (v := getattr(r, field)) is not None), None)

    return DerivedQuake(
        columns={
            "occurred_at": primary.occurred_at,
            "magnitude": primary.magnitude,
            "depth_km": primary.depth_km,
            "region": primary.region,
            "felt": first_present("felt"),
            "potential": first_present("potential"),
            "shakemap_url": first_present("shakemap_url"),
            "source_feeds": [r.feed.value for r in reports],
        },
        latitude=primary.latitude,
        longitude=primary.longitude,
    )


async def upsert_report(
    session: AsyncSession, report: QuakeReport, config: DedupConfig
) -> UpsertOutcome:
    match = await _find_by_fingerprint(session, report.fingerprint)
    if match is None:
        match = await _find_nearby(session, report, config)
    if match is None:
        raw = {report.feed.value: report.raw}
        derived = derive(raw, config.shakemap_base_url)
        inserted = await session.scalar(
            insert(Earthquake)
            .values(
                **derived.columns,
                location=derived.location,
                fingerprint=report.fingerprint,
                raw=raw,
            )
            .on_conflict_do_nothing(index_elements=[Earthquake.fingerprint])
            .returning(Earthquake.id)
        )
        if inserted is not None:
            return UpsertOutcome.INSERTED
        # Lost a race with a concurrent insert of the same fingerprint: merge into it.
        match = await _find_by_fingerprint(session, report.fingerprint)
        if match is None:  # pragma: no cover - the conflicting row cannot vanish under us
            raise RuntimeError(f"fingerprint {report.fingerprint} conflicted but is missing")
    return _merge(match, report, config)


def _merge(match: _Match, report: QuakeReport, config: DedupConfig) -> UpsertOutcome:
    row = match.row
    raw = {**row.raw, report.feed.value: report.raw}
    derived = derive(raw, config.shakemap_base_url)

    changed = False
    for field, value in {**derived.columns, "raw": raw}.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    if (match.latitude, match.longitude) != (derived.latitude, derived.longitude):
        row.location = derived.location
        changed = True
    return UpsertOutcome.UPDATED if changed else UpsertOutcome.UNCHANGED


def _select_match() -> Select[tuple[Earthquake, float, float]]:
    return select(Earthquake, _LATITUDE, _LONGITUDE).with_for_update(of=Earthquake)


async def _find_by_fingerprint(session: AsyncSession, fingerprint: str) -> _Match | None:
    result = await session.execute(_select_match().where(Earthquake.fingerprint == fingerprint))
    return _to_match(result.first())


async def _find_nearby(
    session: AsyncSession, report: QuakeReport, config: DedupConfig
) -> _Match | None:
    point = func.ST_GeogFromText(f"SRID=4326;{report.wkt}")
    seconds_apart = func.abs(func.extract("epoch", Earthquake.occurred_at - report.occurred_at))
    result = await session.execute(
        _select_match()
        .where(
            Earthquake.occurred_at.between(
                report.occurred_at - config.max_time_diff,
                report.occurred_at + config.max_time_diff,
            ),
            func.ST_DWithin(Earthquake.location, point, config.max_distance_m),
        )
        .order_by(seconds_apart, func.ST_Distance(Earthquake.location, point))
        .limit(1)
    )
    return _to_match(result.first())


def _to_match(row: Any) -> _Match | None:
    if row is None:
        return None
    earthquake, latitude, longitude = row
    # Points are stored from 2-decimal BMKG coordinates; round away float noise.
    return _Match(earthquake, Decimal(str(round(latitude, 6))), Decimal(str(round(longitude, 6))))
