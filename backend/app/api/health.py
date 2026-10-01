from fastapi import APIRouter, Response, status

from app.api.deps import EngineDep, RedisDep, SettingsDep
from app.core.health import check_readiness
from app.schemas.health import LivenessResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> LivenessResponse:
    """Liveness: the process is up and serving requests. Touches no dependencies."""
    return LivenessResponse()


@router.get(
    "/readyz",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadinessResponse,
            "description": "PostgreSQL is unreachable",
        }
    },
)
async def readyz(
    response: Response, engine: EngineDep, redis: RedisDep, settings: SettingsDep
) -> ReadinessResponse:
    """Readiness: 200 while PostgreSQL is reachable, even if Redis is down (reported as
    `degraded`); 503 only when the database check fails."""
    result = await check_readiness(
        engine,
        redis,
        db_timeout_seconds=settings.readiness_timeout_seconds,
        redis_timeout_seconds=settings.redis_socket_timeout_seconds,
    )
    if result.db != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
