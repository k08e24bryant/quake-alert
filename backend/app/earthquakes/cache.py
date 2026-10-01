"""Redis cache for the query API. Every operation degrades to "no cache" if Redis fails:
the API must keep answering from the database when Redis is down.

Reads and writes go through the API's circuit breaker (GuardedRedis). While the circuit is
open they skip Redis silently; the breaker logs its own state changes once.
"""

import hashlib
import json
import logging
from enum import StrEnum
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.circuit_breaker import CircuitOpenError
from app.core.redis import GuardedRedis

logger = logging.getLogger(__name__)

LATEST_KEY = "quake-alert:earthquakes:latest"
LIST_KEY_PREFIX = "quake-alert:earthquakes:list:"


class CacheStatus(StrEnum):
    HIT = "HIT"
    MISS = "MISS"
    BYPASS = "BYPASS"  # Redis unavailable; served straight from the database


def list_key(normalized_params: dict[str, Any]) -> str:
    """Same filters in any order or spelling (e.g. lat=-6.2 vs -6.20) -> same key."""
    canonical = json.dumps(normalized_params, sort_keys=True, separators=(",", ":"))
    return LIST_KEY_PREFIX + hashlib.sha256(canonical.encode()).hexdigest()


async def read(redis: GuardedRedis, key: str) -> tuple[str | None, CacheStatus]:
    try:
        value: str | None = await redis.run(lambda r: r.get(key))
    except CircuitOpenError:
        return None, CacheStatus.BYPASS
    except (RedisError, OSError) as exc:
        logger.warning("cache read failed", extra={"key": key, "error": repr(exc)})
        return None, CacheStatus.BYPASS
    return value, CacheStatus.HIT if value is not None else CacheStatus.MISS


async def write(redis: GuardedRedis, key: str, value: str, ttl_seconds: int) -> None:
    try:
        await redis.run(lambda r: r.set(key, value, ex=ttl_seconds))
    except CircuitOpenError:
        return
    except (RedisError, OSError) as exc:
        logger.warning("cache write failed", extra={"key": key, "error": repr(exc)})


async def invalidate_latest(redis: Redis) -> None:
    """Called by ingestion (the worker, with its own connection; no API breaker) after it
    inserts or updates a row."""
    try:
        await redis.delete(LATEST_KEY)
    except (RedisError, OSError) as exc:
        # The TTL bounds how long `latest` can stay stale.
        logger.warning("latest cache invalidation failed", extra={"error": repr(exc)})
