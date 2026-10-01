from redis.asyncio import Redis


def create_redis(url: str) -> Redis:
    redis: Redis = Redis.from_url(url, decode_responses=True)
    return redis


async def ping_redis(redis: Redis) -> None:
    await redis.ping()
