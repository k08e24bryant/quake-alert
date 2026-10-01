"""Real BMKG responses saved from the live API on 2026-10-01 (tests/fixtures/bmkg/), plus
builders for synthetic items in the same shape."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.ingestion.domain import Feed, QuakeReport
from app.ingestion.parser import parse_item

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "bmkg"
BASE_URL = "https://data.bmkg.go.id/DataMKG/TEWS/"

# Distinct quakes across the three samples. autogempa's quake is also the first
# gempadirasakan item; no other item appears in more than one feed.
DISTINCT_QUAKES = 30

DEFAULT_TIME = datetime(2026, 10, 1, 6, 24, 52, tzinfo=UTC)


def load(feed: Feed) -> Any:
    return json.loads((FIXTURES_DIR / f"{feed.value}.json").read_text(encoding="utf-8"))


def make_item(
    *,
    occurred_at: datetime = DEFAULT_TIME,
    magnitude: str | Decimal = "3.2",
    depth_km: int = 25,
    latitude: str | Decimal = "-2.46",
    longitude: str | Decimal = "140.38",
    region: str = "Pusat gempa berada di darat 15 km Barat Laut Sentani",
    potential: str | None = None,
    felt: str | None = None,
    shakemap: str | None = None,
) -> dict[str, str]:
    """A BMKG feed item (all strings, like the real API) for the given values."""
    item = {
        "DateTime": occurred_at.astimezone(UTC).isoformat(),
        "Coordinates": f"{latitude},{longitude}",
        "Magnitude": str(magnitude),
        "Kedalaman": f"{depth_km} km",
        "Wilayah": region,
    }
    optional = {"Potensi": potential, "Dirasakan": felt, "Shakemap": shakemap}
    item.update({key: value for key, value in optional.items() if value is not None})
    return item


def make_report(feed: Feed = Feed.AUTOGEMPA, **fields: Any) -> QuakeReport:
    """A report parsed from make_item(**fields), so its raw payload matches its values."""
    return parse_item(feed, make_item(**fields), BASE_URL)
