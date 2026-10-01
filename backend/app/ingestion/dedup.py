"""Store QuakeReports so that each real quake is one earthquakes row, without ever merging
two distinct quakes.

Safety principle: a wrong merge hides a quake (a missed alert); a duplicate row costs at
most a duplicate alert. When in doubt, insert a new row.

BMKG has no quake ID. A report from feed F resolves to an existing row by the first rule
that matches, and never to a row already `claimed` by another item of the same feed
snapshot (items within one snapshot are always distinct quakes):
  1. Same-feed revision: the row holds an F payload with the identical DateTime (exact
     second) AND that payload's coordinates are within same_feed_revision_max_km of the
     report. Coordinates and other fields may have moved; it is the same quake. Farther
     away it is a different quake that happens to share the second. Several candidates
     (only possible after a snapshot forced duplicates) -> the nearest one.
     DateTime is compared as BMKG's own string, so if BMKG ever changed its format, the
     comparison fails safe: a new row, never a wrong merge.
  2. Exact fingerprint (UTC time to the second + lat/lon rounded to 2 decimals), and
  3. Fuzzy: within dedup_max_time_diff_seconds AND dedup_max_distance_km, nearest in time.
     Rules 2 and 3 are cross-feed only: they never match a row that already holds an F
     payload, because F reporting a different DateTime means F sees a different quake.
Otherwise the report becomes a new row.

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
  - If a stored payload no longer parses, the row is left exactly as it is and the report
    is reported as SKIPPED, so one bad row never fails a whole feed.
  - fingerprint is set by the first report and never changes. When a new row's natural
    fingerprint already belongs to a row it must not merge into, the new row gets a
    salted fingerprint (see _salted_fingerprint), so the key stays unique.
"""

import hashlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from geoalchemy2 import WKTElement
from sqlalchemy import ColumnElement, Float, Select, func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Earthquake
from app.ingestion.domain import FEED_PRECEDENCE, QuakeReport, distance_km
from app.ingestion.parser import BmkgParseError, parse_coordinates, parse_item

logger = logging.getLogger(__name__)

_LATITUDE = func.ST_Y(func.geometry(Earthquake.location), type_=Float)
_LONGITUDE = func.ST_X(func.geometry(Earthquake.location), type_=Float)
_MAX_SALT = 100


class UpsertOutcome(StrEnum):
    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    # Matched a row whose stored payloads no longer parse; nothing was written.
    # Counted in the run's skipped_count.
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class DedupConfig:
    max_time_diff: timedelta
    max_distance_m: float
    shakemap_base_url: str  # needed to re-derive shakemap_url from raw payloads
    same_feed_revision_max_km: float = 100.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "DedupConfig":
        return cls(
            max_time_diff=timedelta(seconds=settings.dedup_max_time_diff_seconds),
            max_distance_m=settings.dedup_max_distance_km * 1000,
            shakemap_base_url=settings.bmkg_base_url,
            same_feed_revision_max_km=settings.same_feed_revision_max_km,
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
    """Compute a row's columns from its per-feed payloads by FEED_PRECEDENCE.

    Raises BmkgParseError if a stored payload does not parse with the current parser.
    """
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
    session: AsyncSession,
    report: QuakeReport,
    config: DedupConfig,
    claimed: set[uuid.UUID] | None = None,
) -> UpsertOutcome:
    """Insert or merge one report.

    `claimed` holds the rows already resolved by earlier items of the same feed snapshot;
    pass one set per snapshot. The resolved row's id is added to it.
    """
    claimed = set() if claimed is None else claimed
    match = await _find_match(session, report, config, claimed)
    if match is None:
        row_id = await _insert(session, report, config)
        claimed.add(row_id)
        return UpsertOutcome.INSERTED
    claimed.add(match.row.id)
    return _merge(match, report, config)


async def _find_match(
    session: AsyncSession, report: QuakeReport, config: DedupConfig, claimed: set[uuid.UUID]
) -> _Match | None:
    feed = report.feed.value
    point = func.ST_GeogFromText(f"SRID=4326;{report.wkt}")
    unclaimed: ColumnElement[bool] = Earthquake.id.not_in(claimed) if claimed else true()
    no_payload_from_this_feed = ~Earthquake.raw.has_key(feed)

    same_datetime_in_this_feed = (
        _select_match()
        .where(Earthquake.raw.contains({feed: {"DateTime": report.raw["DateTime"]}}), unclaimed)
        .order_by(Earthquake.created_at)
    )
    candidates = (await session.execute(same_datetime_in_this_feed)).all()
    revision = _nearest_same_feed_revision(candidates, report, config.same_feed_revision_max_km)
    if revision is not None:
        return revision

    same_fingerprint = _select_match().where(
        Earthquake.fingerprint == report.fingerprint, no_payload_from_this_feed, unclaimed
    )
    seconds_apart = func.abs(func.extract("epoch", Earthquake.occurred_at - report.occurred_at))
    nearby_from_other_feeds = (
        _select_match()
        .where(
            Earthquake.occurred_at.between(
                report.occurred_at - config.max_time_diff,
                report.occurred_at + config.max_time_diff,
            ),
            func.ST_DWithin(Earthquake.location, point, config.max_distance_m),
            no_payload_from_this_feed,
            unclaimed,
        )
        .order_by(seconds_apart, func.ST_Distance(Earthquake.location, point))
        .limit(1)
    )
    for query in (same_fingerprint, nearby_from_other_feeds):
        match = _to_match((await session.execute(query)).first())
        if match is not None:
            return match
    return None


def _nearest_same_feed_revision(
    candidates: Sequence[Any], report: QuakeReport, max_km: float
) -> _Match | None:
    """Among rows holding a payload from the report's feed with the same DateTime, the one
    whose payload is nearest the report, if within max_km.

    Distance is measured to that feed's own previous coordinates, not to the row's location,
    which may come from a higher-precedence feed. A payload whose coordinates can't be read
    is not treated as a match: unconfirmed means a new row, never a guess.
    """
    best: tuple[float, _Match] | None = None
    for candidate in candidates:
        match = _to_match(candidate)
        if match is None:
            continue
        try:
            lat, lon = parse_coordinates(match.row.raw[report.feed.value]["Coordinates"])
        except (BmkgParseError, KeyError, TypeError, AttributeError):
            continue
        distance = distance_km(lat, lon, report.latitude, report.longitude)
        if distance <= max_km and (best is None or distance < best[0]):
            best = (distance, match)
    return best[1] if best else None


async def _insert(session: AsyncSession, report: QuakeReport, config: DedupConfig) -> uuid.UUID:
    raw = {report.feed.value: report.raw}
    derived = derive(raw, config.shakemap_base_url)
    fingerprints = [report.fingerprint] + [
        _salted_fingerprint(report, n) for n in range(1, _MAX_SALT + 1)
    ]
    for fingerprint in fingerprints:
        row_id: uuid.UUID | None = await session.scalar(
            insert(Earthquake)
            .values(**derived.columns, location=derived.location, fingerprint=fingerprint, raw=raw)
            .on_conflict_do_nothing(index_elements=[Earthquake.fingerprint])
            .returning(Earthquake.id)
        )
        if row_id is not None:
            return row_id
    raise RuntimeError(f"no free fingerprint for {report.fingerprint} after {_MAX_SALT} salts")


def _salted_fingerprint(report: QuakeReport, n: int) -> str:
    """For a distinct quake whose natural fingerprint is taken by a row it must not merge
    into (e.g. two items of one snapshot at the same second and rounded position)."""
    return hashlib.sha256(f"{report.fingerprint}|{report.feed.value}|{n}".encode()).hexdigest()


def _merge(match: _Match, report: QuakeReport, config: DedupConfig) -> UpsertOutcome:
    row = match.row
    raw = {**row.raw, report.feed.value: report.raw}
    try:
        derived = derive(raw, config.shakemap_base_url)
    except BmkgParseError as exc:
        logger.error(
            "stored BMKG payload no longer parses; leaving row unchanged",
            extra={"earthquake_id": str(row.id), "feed": report.feed.value, "error": str(exc)},
        )
        return UpsertOutcome.SKIPPED

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


def _to_match(row: Any) -> _Match | None:
    if row is None:
        return None
    earthquake, latitude, longitude = row
    # Points are stored from 2-decimal BMKG coordinates; round away float noise.
    return _Match(earthquake, Decimal(str(round(latitude, 6))), Decimal(str(round(longitude, 6))))
