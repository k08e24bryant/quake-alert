"""Telegram subscriptions: one per chat. Storage only; the bot decides what to call."""

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from geoalchemy2 import WKTElement
from sqlalchemy import Float, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Subscription, SubscriptionChannel

DEFAULT_RADIUS_KM = 200
DEFAULT_MIN_MAGNITUDE = Decimal("4.0")
MIN_RADIUS_KM, MAX_RADIUS_KM = 10, 1000
MIN_MAGNITUDE, MAX_MAGNITUDE = Decimal("2.0"), Decimal("9.0")

_TWO_PLACES = Decimal("0.01")


def round_coordinate(value: float) -> Decimal:
    """Two decimals (~1.1 km): precise enough for a 10 km minimum radius, and the exact
    position the user shared is never stored."""
    return Decimal(str(value)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    id: uuid.UUID
    latitude: Decimal
    longitude: Decimal
    radius_km: int
    min_magnitude: Decimal
    is_active: bool


async def save_location(
    session: AsyncSession, chat_id: int, latitude: float, longitude: float
) -> SubscriptionView:
    """Create the chat's subscription with default settings, or move the existing one
    (keeping its settings). Either way it becomes active: the user is talking to the bot,
    so they have not blocked it."""
    lat, lon = round_coordinate(latitude), round_coordinate(longitude)
    location = WKTElement(f"POINT({lon} {lat})", srid=4326)
    statement = (
        insert(Subscription)
        .values(
            channel=SubscriptionChannel.TELEGRAM,
            telegram_chat_id=chat_id,
            location=location,
            radius_km=DEFAULT_RADIUS_KM,
            min_magnitude=DEFAULT_MIN_MAGNITUDE,
            is_active=True,
        )
        .on_conflict_do_update(
            index_elements=[Subscription.telegram_chat_id],
            set_={"location": location, "is_active": True},
        )
    )
    await session.execute(statement)
    view = await get_for_chat(session, chat_id)
    if view is None:  # upserted in this same transaction; can't happen
        raise RuntimeError("subscription missing right after its upsert")
    return view


async def update_settings(
    session: AsyncSession,
    chat_id: int,
    *,
    radius_km: int | None = None,
    min_magnitude: Decimal | None = None,
) -> SubscriptionView | None:
    """None if the chat has no subscription yet."""
    values: dict[str, object] = {"is_active": True}
    if radius_km is not None:
        values["radius_km"] = radius_km
    if min_magnitude is not None:
        values["min_magnitude"] = min_magnitude
    await session.execute(
        update(Subscription).where(Subscription.telegram_chat_id == chat_id).values(**values)
    )
    return await get_for_chat(session, chat_id)


async def get_for_chat(session: AsyncSession, chat_id: int) -> SubscriptionView | None:
    row = (
        await session.execute(
            select(
                Subscription.id,
                func.ST_Y(func.geometry(Subscription.location), type_=Float),
                func.ST_X(func.geometry(Subscription.location), type_=Float),
                Subscription.radius_km,
                Subscription.min_magnitude,
                Subscription.is_active,
            ).where(Subscription.telegram_chat_id == chat_id)
        )
    ).first()
    if row is None:
        return None
    sub_id, lat, lon, radius_km, min_magnitude, is_active = row
    return SubscriptionView(
        id=sub_id,
        latitude=round_coordinate(lat),
        longitude=round_coordinate(lon),
        radius_km=radius_km,
        min_magnitude=min_magnitude,
        is_active=is_active,
    )


async def delete_for_chat(session: AsyncSession, chat_id: int) -> int:
    """Hard delete. Deliveries go with it (ON DELETE CASCADE), pending ones included, so a
    queued alert job finds nothing to send."""
    deleted = await session.scalars(
        delete(Subscription)
        .where(Subscription.telegram_chat_id == chat_id)
        .returning(Subscription.id)
    )
    return len(deleted.all())


async def deactivate(session: AsyncSession, subscription_id: uuid.UUID) -> None:
    await session.execute(
        update(Subscription).where(Subscription.id == subscription_id).values(is_active=False)
    )
