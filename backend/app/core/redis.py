from redis.asyncio import Redis


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
