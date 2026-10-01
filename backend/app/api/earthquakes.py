import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import GuardedRedisDep, SessionDep, SettingsDep
from app.core.rate_limit import enforce_rate_limit
from app.earthquakes.cache import CacheStatus
from app.earthquakes.service import EarthquakeService
from app.schemas.earthquakes import EarthquakeDetail, EarthquakeList, EarthquakeQuery

router = APIRouter(
    prefix="/v1/earthquakes",
    tags=["earthquakes"],
    dependencies=[Depends(enforce_rate_limit)],
    responses={status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded"}},
)


def get_service(
    session: SessionDep, redis: GuardedRedisDep, settings: SettingsDep
) -> EarthquakeService:
    return EarthquakeService(session, redis, settings)


ServiceDep = Annotated[EarthquakeService, Depends(get_service)]


def _mark_cache(response: Response, cache_status: CacheStatus) -> None:
    response.headers["X-Cache"] = cache_status.value


@router.get("")
async def list_earthquakes(
    params: Annotated[EarthquakeQuery, Query()], service: ServiceDep, response: Response
) -> EarthquakeList:
    """Earthquakes, newest first, with optional geospatial, magnitude and time filters.

    Page with `next_cursor`. `distance_km` is filled when `lat` and `lon` are given.
    """
    result, cache_status = await service.list(params)
    _mark_cache(response, cache_status)
    return result


@router.get("/latest", responses={status.HTTP_404_NOT_FOUND: {"description": "No quakes yet"}})
async def latest_earthquake(service: ServiceDep, response: Response) -> EarthquakeDetail:
    """The most recent earthquake by origin time."""
    result, cache_status = await service.latest()
    _mark_cache(response, cache_status)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No earthquakes recorded yet.")
    return result


@router.get("/{earthquake_id}", responses={status.HTTP_404_NOT_FOUND: {"description": "Unknown"}})
async def get_earthquake(earthquake_id: uuid.UUID, service: ServiceDep) -> EarthquakeDetail:
    result = await service.get(earthquake_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Earthquake not found.")
    return result
