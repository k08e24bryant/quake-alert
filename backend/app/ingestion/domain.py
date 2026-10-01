import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

_TWO_PLACES = Decimal("0.01")
_EARTH_MEAN_RADIUS_KM = 6371.0088


class Feed(StrEnum):
    """BMKG TEWS feeds. Values are the file names without the .json suffix."""

    AUTOGEMPA = "autogempa"  # latest single quake
    GEMPATERKINI = "gempaterkini"  # latest 15, M5+
    GEMPADIRASAKAN = "gempadirasakan"  # latest 15 felt quakes


# When feeds disagree about a quake, the earlier feed in this tuple wins.
FEED_PRECEDENCE: tuple[Feed, ...] = (Feed.AUTOGEMPA, Feed.GEMPATERKINI, Feed.GEMPADIRASAKAN)


@dataclass(frozen=True, slots=True)
class QuakeReport:
    """One quake as reported by one feed, parsed into proper types."""

    feed: Feed
    occurred_at: datetime  # timezone-aware, UTC
    magnitude: Decimal
    depth_km: int
    latitude: Decimal
    longitude: Decimal
    region: str
    # BMKG's free-text "Potensi", verbatim. NOT necessarily tsunami information.
    potential: str | None
    felt: str | None
    shakemap_url: str | None
    raw: dict[str, Any]

    @property
    def fingerprint(self) -> str:
        return compute_fingerprint(self.occurred_at, self.latitude, self.longitude)

    @property
    def wkt(self) -> str:
        return f"POINT({self.longitude} {self.latitude})"


def compute_fingerprint(occurred_at: datetime, latitude: Decimal, longitude: Decimal) -> str:
    """sha256 of the UTC time to the second plus lat/lon rounded to 2 decimals."""
    timestamp = occurred_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lat = latitude.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    lon = longitude.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    return hashlib.sha256(f"{timestamp}|{lat}|{lon}".encode()).hexdigest()


def distance_km(lat1: Decimal, lon1: Decimal, lat2: Decimal, lon2: Decimal) -> float:
    """Great-circle (haversine) distance. Within ~0.5% of PostGIS geography distance, which is
    plenty for a coarse guard like SAME_FEED_REVISION_MAX_KM."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_MEAN_RADIUS_KM * math.asin(math.sqrt(a))
