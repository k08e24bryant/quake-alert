"""Real BMKG responses saved from the live API on 2026-10-01 (tests/fixtures/bmkg/)."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.ingestion.domain import Feed, QuakeReport

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "bmkg"
BASE_URL = "https://data.bmkg.go.id/DataMKG/TEWS/"

# Distinct quakes across the three samples. autogempa's quake is also the first
# gempadirasakan item; no other item appears in more than one feed.
DISTINCT_QUAKES = 30


def load(feed: Feed) -> Any:
    return json.loads((FIXTURES_DIR / f"{feed.value}.json").read_text(encoding="utf-8"))


def make_report(**overrides: Any) -> QuakeReport:
    """A plausible report (the autogempa sample) with fields overridden."""
    report = QuakeReport(
        feed=Feed.AUTOGEMPA,
        occurred_at=datetime(2026, 10, 1, 6, 24, 52, tzinfo=UTC),
        magnitude=Decimal("3.2"),
        depth_km=25,
        latitude=Decimal("-2.46"),
        longitude=Decimal("140.38"),
        region="Pusat gempa berada di darat 15 km Barat Laut Sentani",
        tsunami_potential=None,
        felt=None,
        shakemap_url=None,
        raw={"sample": True},
    )
    return replace(report, **overrides)
