"""Turns BMKG TEWS JSON into QuakeReports.

Built from real responses saved in tests/fixtures/bmkg/. Shape:
    {"Infogempa": {"gempa": <item>}}         autogempa: a single object
    {"Infogempa": {"gempa": [<item>, ...]}}  gempaterkini, gempadirasakan: a list

Item fields used here (all strings): DateTime (ISO 8601, UTC), Coordinates ("lat,lon"),
Magnitude ("5.2"), Kedalaman ("10 km"), Wilayah, and optionally Potensi (not in
gempadirasakan), Dirasakan (not in gempaterkini), Shakemap (autogempa only, a file name).
Tanggal/Jam (local WIB time) and Lintang/Bujur (duplicate Coordinates) are ignored.
"""

import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ingestion.domain import Feed, QuakeReport

logger = logging.getLogger(__name__)

_DEPTH_RE = re.compile(r"^\s*(\d+)\s*km\s*$", re.IGNORECASE)
_ONE_PLACE = Decimal("0.1")


class BmkgParseError(ValueError):
    """The payload or one of its items does not have the expected shape."""


def parse_feed(feed: Feed, payload: Any, shakemap_base_url: str) -> list[QuakeReport]:
    """Parse a whole feed. Malformed items are logged and skipped; a payload whose
    envelope is wrong raises BmkgParseError."""
    try:
        items = payload["Infogempa"]["gempa"]
    except (KeyError, TypeError) as exc:
        raise BmkgParseError(f"{feed}: missing Infogempa.gempa") from exc
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise BmkgParseError(f"{feed}: Infogempa.gempa is {type(items).__name__}")

    reports = []
    for index, item in enumerate(items):
        try:
            reports.append(parse_item(feed, item, shakemap_base_url))
        except BmkgParseError as exc:
            logger.warning(
                "skipping malformed BMKG item",
                extra={"feed": feed.value, "index": index, "error": str(exc)},
            )
    return reports


def parse_item(feed: Feed, item: Any, shakemap_base_url: str) -> QuakeReport:
    if not isinstance(item, dict):
        raise BmkgParseError(f"item is {type(item).__name__}, expected object")
    latitude, longitude = parse_coordinates(_required(item, "Coordinates"))
    shakemap = _optional(item, "Shakemap")
    return QuakeReport(
        feed=feed,
        occurred_at=parse_datetime(_required(item, "DateTime")),
        magnitude=parse_magnitude(_required(item, "Magnitude")),
        depth_km=parse_depth_km(_required(item, "Kedalaman")),
        latitude=latitude,
        longitude=longitude,
        region=_required(item, "Wilayah"),
        tsunami_potential=_optional(item, "Potensi"),
        felt=_optional(item, "Dirasakan"),
        shakemap_url=f"{shakemap_base_url.rstrip('/')}/{shakemap}" if shakemap else None,
        raw=item,
    )


def parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise BmkgParseError(f"invalid DateTime {value!r}") from exc
    if parsed.tzinfo is None:
        raise BmkgParseError(f"DateTime {value!r} has no UTC offset")
    return parsed.astimezone(UTC)


def parse_coordinates(value: str) -> tuple[Decimal, Decimal]:
    parts = value.split(",")
    if len(parts) != 2:
        raise BmkgParseError(f"invalid Coordinates {value!r}, expected 'lat,lon'")
    latitude, longitude = (_decimal(part, "Coordinates") for part in parts)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise BmkgParseError(f"Coordinates {value!r} out of range")
    return latitude, longitude


def parse_magnitude(value: str) -> Decimal:
    magnitude = _decimal(value, "Magnitude")
    if not 0 <= magnitude < 10:
        raise BmkgParseError(f"Magnitude {value!r} out of range")
    return magnitude.quantize(_ONE_PLACE)


def parse_depth_km(value: str) -> int:
    match = _DEPTH_RE.match(value)
    if match is None:
        raise BmkgParseError(f"invalid Kedalaman {value!r}, expected '<n> km'")
    return int(match.group(1))


def _decimal(value: str, field: str) -> Decimal:
    try:
        result = Decimal(value.strip())
    except InvalidOperation as exc:
        raise BmkgParseError(f"invalid {field} {value!r}") from exc
    if not result.is_finite():
        raise BmkgParseError(f"invalid {field} {value!r}")
    return result


def _required(item: dict[str, Any], key: str) -> str:
    value = _optional(item, key)
    if value is None:
        raise BmkgParseError(f"missing {key}")
    return value


def _optional(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BmkgParseError(f"{key} is {type(value).__name__}, expected string")
    return value.strip() or None
