"""Dev only: insert a synthetic quake near a point and run alert matching, to try the whole
alert path with your own dev bot.

    cd backend
    uv run python -m scripts.dev_fake_quake --lat -6.21 --lon 106.85 --magnitude 5.0

Hard guards, all checked before anything is written, in this order:
  1. ENVIRONMENT=development;
  2. TELEGRAM_BOT_TOKEN is set;
  3. getMe says the token belongs to a bot whose username ends with "_dev_bot", so a real
     bot's subscribers can never get a test alert.

The row is flagged is_synthetic: it is never in any public API response, never a dedup
match for a real report, and its alert starts with "[TES - BUKAN GEMPA NYATA]". The script
creates the deliveries; the worker (docker compose) sends them.
"""

import argparse
import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from arq import create_pool
from arq.connections import RedisSettings
from geoalchemy2 import WKTElement
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.models import Earthquake
from app.db.session import create_engine, create_sessionmaker
from app.notifications.matcher import match_flagged
from app.notifications.subscriptions import round_coordinate
from app.notifications.telegram import TelegramClient, TelegramError
from worker.jobs import enqueue_delivery

DEV_BOT_SUFFIX = "_dev_bot"
_KM_PER_DEGREE_LATITUDE = 111.32


class GuardError(Exception):
    """A safety guard refused to run. Nothing was written."""


@dataclass(frozen=True, slots=True)
class FakeQuake:
    earthquake_id: uuid.UUID
    latitude: Decimal
    longitude: Decimal
    bot_username: str
    delivery_ids: list[uuid.UUID]


async def check_guards(settings: Settings, telegram: TelegramClient) -> str:
    """Return the dev bot's username, or raise GuardError."""
    if settings.environment != "development":
        raise GuardError(
            f"refusing: ENVIRONMENT is {settings.environment!r}; this only runs in 'development'"
        )
    if not settings.telegram_bot_token.get_secret_value():
        raise GuardError("refusing: TELEGRAM_BOT_TOKEN is not set")
    try:
        me = await telegram.get_me()
    except TelegramError as exc:
        raise GuardError(
            f"refusing: could not verify the bot with getMe ({exc.description})"
        ) from None
    username = me.get("username")
    # Telegram usernames are case-insensitive.
    if not isinstance(username, str) or not username.lower().endswith(DEV_BOT_SUFFIX):
        raise GuardError(
            f"refusing: the token belongs to @{username}, not a dev bot "
            f"(its username must end with {DEV_BOT_SUFFIX!r})"
        )
    return username


async def create_fake_quake(
    settings: Settings,
    telegram: TelegramClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    latitude: float,
    longitude: float,
    magnitude: Decimal,
    depth_km: int,
    offset_km: float,
    now: datetime,
) -> FakeQuake:
    """Insert the synthetic quake `offset_km` north of the point and match it, in one
    transaction (the same outbox path as real quakes)."""
    username = await check_guards(settings, telegram)
    lat = round_coordinate(min(latitude + offset_km / _KM_PER_DEGREE_LATITUDE, 90.0))
    lon = round_coordinate(longitude)
    row_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            Earthquake(
                id=row_id,
                occurred_at=now,
                magnitude=magnitude,
                depth_km=depth_km,
                location=WKTElement(f"POINT({lon} {lat})", srid=4326),
                region=f"Gempa sintetis untuk pengujian, dekat {lat}, {lon}",
                potential=None,
                felt=None,
                shakemap_url=None,
                source_feeds=[],
                fingerprint=f"synthetic:{row_id}",
                raw={},
                needs_matching=True,
                is_synthetic=True,
            )
        )
        await session.flush()
        result = await match_flagged(
            session, now=now, max_age=timedelta(minutes=settings.notify_max_age_minutes)
        )
    return FakeQuake(row_id, lat, lon, username, result.delivery_ids)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--magnitude", type=Decimal, default=Decimal("5.0"))
    parser.add_argument("--depth-km", type=int, default=10)
    parser.add_argument(
        "--offset-km", type=float, default=10.0, help="put the quake this far north of the point"
    )
    return parser.parse_args()


async def _main(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient() as http:
            fake = await create_fake_quake(
                settings,
                TelegramClient.from_settings(http, settings),
                create_sessionmaker(engine),
                latitude=args.lat,
                longitude=args.lon,
                magnitude=args.magnitude,
                depth_km=args.depth_km,
                offset_km=args.offset_km,
                now=datetime.now(UTC).replace(microsecond=0),
            )
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        try:
            for delivery_id in fake.delivery_ids:
                await enqueue_delivery(redis, delivery_id)
        finally:
            await redis.aclose()
    finally:
        await engine.dispose()
    print(
        f"synthetic quake {fake.earthquake_id} at {fake.latitude}, {fake.longitude} "
        f"(bot @{fake.bot_username}): {len(fake.delivery_ids)} alert(s) queued for the worker"
    )


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_main(args))
    except GuardError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
