"""Per-client-IP fixed-window rate limiting in Redis.

Reads fail open: if Redis is unreachable, requests are allowed, because the API must keep
serving from the database when Redis is down. Writes to webhook subscriptions fail
closed: without a working limiter they answer 503, because each one makes us send a
request to a URL of the caller's choosing (see README). Calls go through the API's circuit
breaker (GuardedRedis): while it is open, Redis is not touched and nothing is logged.
"""

import ipaddress
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.circuit_breaker import CircuitOpenError
from app.core.redis import GuardedRedis

logger = logging.getLogger(__name__)

KEY_PREFIX = "quake-alert:ratelimit:"


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int  # until the current window ends

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_seconds),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.reset_seconds)
        return headers


class RateLimiter:
    def __init__(
        self,
        redis: GuardedRedis,
        *,
        limit: int,
        window_seconds: int = 60,
        name: str = "",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        # Separate limiters (e.g. the hourly one for subscription writes) need their own keys.
        self._prefix = f"{KEY_PREFIX}{name}:" if name else KEY_PREFIX
        self._limit = limit
        self._window = window_seconds
        self._clock = clock

    async def hit(self, client: str) -> RateLimitDecision | None:
        """Count one request from `client`. None means Redis failed: let it through."""
        now = self._clock()
        window = int(now // self._window)
        key = f"{self._prefix}{client}:{window}"

        async def count_hit(redis: Redis) -> int:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self._window, nx=True)
                count, _ = await pipe.execute()
            return int(count)

        try:
            count = await self._redis.run(count_hit)
        except CircuitOpenError:
            return None
        except (RedisError, OSError) as exc:
            logger.warning("rate limiter unavailable, allowing request", extra={"error": repr(exc)})
            return None
        reset = max(1, math.ceil((window + 1) * self._window - now))
        return RateLimitDecision(
            allowed=count <= self._limit,
            limit=self._limit,
            remaining=max(0, self._limit - count),
            reset_seconds=reset,
        )


def client_ip(request: Request, trust_proxy_headers: bool) -> str:
    """The client's IP. X-Forwarded-For is only read when trusted, and then its LAST entry:
    that is the one our proxy (Caddy) appended; earlier entries are client-controlled."""
    if trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        candidate = forwarded.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(request: Request, response: Response) -> None:
    """FastAPI dependency for /v1 read routes: sets X-RateLimit-* headers, raises 429 when
    over. Fails open."""
    await _enforce(request.app.state.rate_limiter, request, response, fail_closed=False)


async def enforce_rate_limit_fail_closed(request: Request, response: Response) -> None:
    """The general /v1 limit for webhook subscription writes: like enforce_rate_limit, but
    503 while the limiter is unavailable (Redis down or breaker open)."""
    await _enforce(request.app.state.rate_limiter, request, response, fail_closed=True)


async def enforce_subscription_write_limit(request: Request, response: Response) -> None:
    """The stricter hourly limit for creating webhook subscriptions, test payloads and
    verification retries, on top of the general one. Its headers replace the general
    ones. Fails closed."""
    await _enforce(
        request.app.state.subscription_write_limiter, request, response, fail_closed=True
    )


async def _enforce(
    limiter: "RateLimiter", request: Request, response: Response, *, fail_closed: bool
) -> None:
    settings = request.app.state.settings
    decision = await limiter.hit(client_ip(request, settings.trust_proxy_headers))
    if decision is None:
        if fail_closed:
            # Worth retrying once the breaker lets the next trial call through.
            retry_after = math.ceil(settings.redis_breaker_open_seconds)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiting is unavailable, so subscription changes are paused. "
                "Retry after the number of seconds in Retry-After.",
                headers={"Retry-After": str(retry_after)},
            )
        return
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Retry after the number of seconds in Retry-After.",
            headers=decision.headers(),
        )
    response.headers.update(decision.headers())
