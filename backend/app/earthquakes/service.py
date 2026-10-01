"""Read side of earthquakes: list/latest/detail queries, with Redis caching."""

import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import ColumnElement, DateTime, Float, Select, Uuid, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.config import Settings
from app.db.models import Earthquake as EarthquakeRow
from app.earthquakes import cache
from app.earthquakes.cache import CacheStatus
from app.earthquakes.pagination import Cursor
from app.schemas.earthquakes import Earthquake, EarthquakeDetail, EarthquakeList, EarthquakeQuery

_NEWEST_FIRST = (EarthquakeRow.occurred_at.desc(), EarthquakeRow.id.desc())


def _query_point(lat: float, lon: float) -> ColumnElement[Any]:
    """The query point as geography, so ST_DWithin/ST_Distance work in meters on the
    spheroid and ST_DWithin can use the GIST index on earthquakes.location."""
    return func.geography(func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326))


_Column = ColumnElement[Any] | InstrumentedAttribute[Any]


def _columns(point: ColumnElement[Any] | None) -> list[_Column]:
    columns: list[_Column] = [
        EarthquakeRow.id,
        EarthquakeRow.occurred_at,
        EarthquakeRow.magnitude,
        EarthquakeRow.depth_km,
        func.ST_Y(func.geometry(EarthquakeRow.location), type_=Float).label("latitude"),
        func.ST_X(func.geometry(EarthquakeRow.location), type_=Float).label("longitude"),
        EarthquakeRow.region,
        EarthquakeRow.potential,
        EarthquakeRow.felt,
        EarthquakeRow.shakemap_url,
        EarthquakeRow.source_feeds,
    ]
    if point is not None:
        distance_m = func.ST_Distance(EarthquakeRow.location, point, type_=Float)
        columns.append((distance_m / 1000).label("distance_km"))
    return columns


def build_list_query(params: EarthquakeQuery) -> Select[Any]:
    """One page plus one extra row (to know whether a next page exists)."""
    point = None
    if params.lat is not None and params.lon is not None:
        point = _query_point(params.lat, params.lon)

    query = select(*_columns(point))
    if point is not None and params.radius_km is not None:
        query = query.where(func.ST_DWithin(EarthquakeRow.location, point, params.radius_km * 1000))
    if params.min_mag is not None:
        query = query.where(EarthquakeRow.magnitude >= params.min_mag)
    if params.max_mag is not None:
        query = query.where(EarthquakeRow.magnitude <= params.max_mag)
    if params.start is not None:
        query = query.where(EarthquakeRow.occurred_at >= params.start)
    if params.end is not None:
        query = query.where(EarthquakeRow.occurred_at < params.end)
    if (cursor := params.decoded_cursor) is not None:
        query = query.where(_after(cursor))
    return query.order_by(*_NEWEST_FIRST).limit(params.limit + 1)


def _after(cursor: Cursor) -> ColumnElement[bool]:
    """Rows strictly after the cursor in (occurred_at DESC, id DESC) order. A row-value
    comparison, which PostgreSQL matches against ix_earthquakes_occurred_at_id."""
    return tuple_(EarthquakeRow.occurred_at, EarthquakeRow.id) < tuple_(
        literal(cursor.occurred_at, DateTime(timezone=True)), literal(cursor.id, Uuid())
    )


class EarthquakeService:
    def __init__(self, session: AsyncSession, redis: Redis, settings: Settings) -> None:
        self._session = session
        self._redis = redis
        self._settings = settings

    async def list(self, params: EarthquakeQuery) -> tuple[EarthquakeList, CacheStatus]:
        key = cache.list_key(params.model_dump(mode="json"))
        cached, status = await cache.read(self._redis, key)
        if cached is not None:
            return EarthquakeList.model_validate_json(cached), status

        rows = (await self._session.execute(build_list_query(params))).mappings().all()
        page = [Earthquake.model_validate(row) for row in rows[: params.limit]]
        next_cursor = None
        if len(rows) > params.limit:
            last = page[-1]
            next_cursor = Cursor(last.occurred_at, last.id).encode()
        result = EarthquakeList(data=page, next_cursor=next_cursor)

        if status is not CacheStatus.BYPASS:
            await cache.write(
                self._redis, key, result.model_dump_json(), self._settings.cache_list_ttl_seconds
            )
        return result, status

    async def latest(self) -> tuple[EarthquakeDetail | None, CacheStatus]:
        cached, status = await cache.read(self._redis, cache.LATEST_KEY)
        if cached is not None:
            return EarthquakeDetail.model_validate_json(cached), status

        row = (
            (await self._session.execute(select(*_columns(None)).order_by(*_NEWEST_FIRST).limit(1)))
            .mappings()
            .first()
        )
        if row is None:
            return None, status
        result = EarthquakeDetail(data=Earthquake.model_validate(row))
        if status is not CacheStatus.BYPASS:
            await cache.write(
                self._redis,
                cache.LATEST_KEY,
                result.model_dump_json(),
                self._settings.cache_latest_ttl_seconds,
            )
        return result, status

    async def get(self, earthquake_id: uuid.UUID) -> EarthquakeDetail | None:
        row = (
            (
                await self._session.execute(
                    select(*_columns(None)).where(EarthquakeRow.id == earthquake_id)
                )
            )
            .mappings()
            .first()
        )
        return EarthquakeDetail(data=Earthquake.model_validate(row)) if row else None
