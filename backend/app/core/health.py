import asyncio
import logging
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.redis import ping_redis
from app.db.session import ping_database
from app.schemas.health import CheckStatus, ReadinessResponse

logger = logging.getLogger(__name__)


async def _run_check(
    name: str, check: Callable[[], Awaitable[None]], timeout_seconds: float
) -> tuple[str, CheckStatus]:
    try:
        async with asyncio.timeout(timeout_seconds):
            await check()
    except Exception as exc:
        logger.warning(
            "readiness check failed",
            extra={"check": name, "error_type": type(exc).__name__, "error": str(exc)},
        )
        return name, "error"
    return name, "ok"


async def check_readiness(
    engine: AsyncEngine, redis: Redis, timeout_seconds: float
) -> ReadinessResponse:
    results = await asyncio.gather(
        _run_check("database", lambda: ping_database(engine), timeout_seconds),
        _run_check("redis", lambda: ping_redis(redis), timeout_seconds),
    )
    checks = dict(results)
    status: CheckStatus = "ok" if all(v == "ok" for v in checks.values()) else "error"
    return ReadinessResponse(status=status, checks=checks)
