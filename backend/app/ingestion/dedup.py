"""Store QuakeReports so that each real quake is exactly one earthquakes row.

BMKG has no quake ID, and the same quake shows up in several feeds with slightly different
fields. A report matches an existing row when either:
  1. its fingerprint (UTC time to the second + lat/lon rounded to 2 decimals) is equal, or
  2. the nearest row in time is within dedup_max_time_diff_seconds AND
     dedup_max_distance_km of it (catches revised times/positions).

Merge rules when a report matches a row:
  - measurements (occurred_at, magnitude, depth, location) and region: the report wins,
    since a later report may carry a revision;
  - tsunami_potential, felt, shakemap_url: the report wins only when it has a value, because
    each is published by just some of the feeds;
  - source_feeds: union; raw: latest raw item per feed;
  - fingerprint: kept from the first report, so the unique key never moves.
"""

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
from app.ingestion.domain import QuakeReport

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

    @classmethod
    def from_settings(cls, settings: Settings) -> "DedupConfig":
        return cls(
            max_time_diff=timedelta(seconds=settings.dedup_max_time_diff_seconds),
            max_distance_m=settings.dedup_max_distance_km * 1000,
        )


@dataclass(slots=True)
class _Match:
    row: Earthquake
    latitude: Decimal
    longitude: Decimal


async def upsert_report(
    session: AsyncSession, report: QuakeReport, config: DedupConfig
) -> UpsertOutcome:
    match = await _find_by_fingerprint(session, report.fingerprint)
    if match is None:
        match = await _find_nearby(session, report, config)
    if match is None:
        inserted = await session.scalar(
            insert(Earthquake)
            .values(**_insert_values(report))
            .on_conflict_do_nothing(index_elements=[Earthquake.fingerprint])
            .returning(Earthquake.id)
        )
        if inserted is not None:
            return UpsertOutcome.INSERTED
        # Lost a race with a concurrent insert of the same fingerprint: merge into it.
        match = await _find_by_fingerprint(session, report.fingerprint)
        if match is None:  # pragma: no cover - the conflicting row cannot vanish under us
            raise RuntimeError(f"fingerprint {report.fingerprint} conflicted but is missing")
    return _merge(match, report)


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


def _insert_values(report: QuakeReport) -> dict[str, Any]:
    return {
        "occurred_at": report.occurred_at,
        "magnitude": report.magnitude,
        "depth_km": report.depth_km,
        "location": WKTElement(report.wkt, srid=4326),
        "region": report.region,
        "tsunami_potential": report.tsunami_potential,
        "felt": report.felt,
        "shakemap_url": report.shakemap_url,
        "source_feeds": [report.feed.value],
        "fingerprint": report.fingerprint,
        "raw": {report.feed.value: report.raw},
    }


def _merge(match: _Match, report: QuakeReport) -> UpsertOutcome:
    row = match.row
    merged: dict[str, Any] = {
        "occurred_at": report.occurred_at,
        "magnitude": report.magnitude,
        "depth_km": report.depth_km,
        "region": report.region,
        "tsunami_potential": report.tsunami_potential or row.tsunami_potential,
        "felt": report.felt or row.felt,
        "shakemap_url": report.shakemap_url or row.shakemap_url,
        "source_feeds": (
            row.source_feeds
            if report.feed.value in row.source_feeds
            else [*row.source_feeds, report.feed.value]
        ),
        "raw": {**row.raw, report.feed.value: report.raw},
    }
    changed = False
    for field, value in merged.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    if (match.latitude, match.longitude) != (report.latitude, report.longitude):
        row.location = WKTElement(report.wkt, srid=4326)
        changed = True
    return UpsertOutcome.UPDATED if changed else UpsertOutcome.UNCHANGED
