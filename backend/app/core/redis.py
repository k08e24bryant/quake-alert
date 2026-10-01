from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

from app.core.circuit_breaker import CircuitBreaker


def create_redis(url: str, timeout_seconds: float) -> Redis:
    redis: Redis = Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=timeout_seconds,
        socket_timeout=timeout_seconds,
    )
    return redis


async def ping_redis(redis: Redis) -> None:
    await redis.ping()


class GuardedRedis:
    """The API's Redis client behind a circuit breaker. All request-path Redis calls (cache,
    rate limiting) go through `run`; when the circuit is open they raise CircuitOpenError
    without touching Redis. /readyz deliberately uses the raw client to really probe it."""

    def __init__(self, redis: Redis, breaker: CircuitBreaker) -> None:
        self.redis = redis
        self.breaker = breaker

    async def run[T](self, operation: Callable[[Redis], Awaitable[T]]) -> T:
        return await self.breaker.call(lambda: operation(self.redis))
