import asyncio
import logging
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.redis import ping_redis
from app.db.session import ping_database
from app.schemas.health import ReadinessResponse

logger = logging.getLogger(__name__)


async def _passes(name: str, check: Callable[[], Awaitable[None]], timeout_seconds: float) -> bool:
    try:
        async with asyncio.timeout(timeout_seconds):
            await check()
    except Exception as exc:
        logger.warning(
            "readiness check failed",
            extra={"check": name, "error_type": type(exc).__name__, "error": str(exc)},
        )
        return False
    return True


async def check_readiness(
    engine: AsyncEngine,
    redis: Redis,
    *,
    db_timeout_seconds: float,
    redis_timeout_seconds: float,
) -> ReadinessResponse:
    """The API is ready when PostgreSQL answers. Redis is probed directly (not through the
    circuit breaker, so it reports the truth) but only marks the API degraded: without it
    the API still serves every request from the database."""
    db_ok, redis_ok = await asyncio.gather(
        _passes("db", lambda: ping_database(engine), db_timeout_seconds),
        _passes("redis", lambda: ping_redis(redis), redis_timeout_seconds),
    )
    return ReadinessResponse(
        db="ok" if db_ok else "error",
        redis="ok" if redis_ok else "degraded",
    )
