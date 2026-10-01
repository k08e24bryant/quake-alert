"""Insert earthquakes rows directly, for API tests that need exact data (not ingestion)."""

import uuid
from datetime import datetime
from decimal import Decimal

from geoalchemy2 import WKTElement
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Earthquake, Subscription, SubscriptionChannel
from app.ingestion.domain import Feed
from tests.bmkg_samples import DEFAULT_TIME, make_item

# Reference points (lat, lon). Distances between them are well known; see the tests.
JAKARTA = (-6.2088, 106.8456)
BOGOR = (-6.5971, 106.8060)
BANDUNG = (-6.9175, 107.6191)
SURABAYA = (-7.2575, 112.7521)


async def seed_quake(
    session: AsyncSession,
    *,
    at: tuple[float, float] = JAKARTA,
    occurred_at: datetime = DEFAULT_TIME,
    magnitude: str = "5.0",
    depth_km: int = 10,
    region: str = "test region",
    potential: str | None = None,
    felt: str | None = None,
    feed: Feed = Feed.GEMPATERKINI,
    needs_matching: bool = False,
    is_synthetic: bool = False,
) -> Earthquake:
    lat, lon = at
    item = make_item(
        occurred_at=occurred_at,
        magnitude=magnitude,
        depth_km=depth_km,
        latitude=str(lat),
        longitude=str(lon),
        region=region,
        potential=potential,
        felt=felt,
    )
    row = Earthquake(
        occurred_at=occurred_at,
        magnitude=Decimal(magnitude),
        depth_km=depth_km,
        location=WKTElement(f"POINT({lon} {lat})", srid=4326),
        region=region,
        potential=potential,
        felt=felt,
        shakemap_url=None,
        source_feeds=[feed.value],
        fingerprint=uuid.uuid4().hex,  # rows here may deliberately share time and place
        raw={feed.value: item},
        needs_matching=needs_matching,
        is_synthetic=is_synthetic,
    )
    session.add(row)
    await session.flush()
    return row


async def seed_subscription(
    session: AsyncSession,
    *,
    at: tuple[float, float] = JAKARTA,
    radius_km: int = 200,
    min_magnitude: str = "4.0",
    is_active: bool = True,
    chat_id: int | None = None,
) -> Subscription:
    lat, lon = at
    row = Subscription(
        channel=SubscriptionChannel.TELEGRAM,
        telegram_chat_id=chat_id if chat_id is not None else uuid.uuid4().int % 10**12,
        location=WKTElement(f"POINT({lon} {lat})", srid=4326),
        radius_km=radius_km,
        min_magnitude=Decimal(min_magnitude),
        is_active=is_active,
    )
    session.add(row)
    await session.flush()
    return row


async def seed_webhook_subscription(
    session: AsyncSession,
    *,
    url: str,
    encrypted_secret: str,
    at: tuple[float, float] = JAKARTA,
    radius_km: int = 200,
    min_magnitude: str = "4.0",
    is_active: bool = True,
    manage_token_hash: str = "0" * 64,
) -> Subscription:
    lat, lon = at
    row = Subscription(
        channel=SubscriptionChannel.WEBHOOK,
        webhook_url=url,
        webhook_secret_encrypted=encrypted_secret,
        manage_token_hash=manage_token_hash,
        location=WKTElement(f"POINT({lon} {lat})", srid=4326),
        radius_km=radius_km,
        min_magnitude=Decimal(min_magnitude),
        is_active=is_active,
    )
    session.add(row)
    await session.flush()
    return row
