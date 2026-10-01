from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.ingestion.domain import compute_fingerprint, distance_km

WHEN = datetime(2026, 10, 1, 6, 24, 52, tzinfo=UTC)
LAT, LON = Decimal("-2.46"), Decimal("140.38")


def test_fingerprint_is_stable_and_hex_sha256() -> None:
    fingerprint = compute_fingerprint(WHEN, LAT, LON)

    assert fingerprint == compute_fingerprint(WHEN, LAT, LON)
    assert len(fingerprint) == 64


def test_fingerprint_ignores_sub_second_time_and_the_offset_used() -> None:
    wib = timezone(timedelta(hours=7))
    same_instant_in_wib = WHEN.astimezone(wib) + timedelta(milliseconds=400)

    assert compute_fingerprint(same_instant_in_wib, LAT, LON) == compute_fingerprint(WHEN, LAT, LON)


def test_fingerprint_rounds_coordinates_to_two_decimals() -> None:
    assert compute_fingerprint(WHEN, Decimal("-2.4649"), Decimal("140.3800")) == (
        compute_fingerprint(WHEN, LAT, LON)
    )
    assert compute_fingerprint(WHEN, Decimal("-2.47"), LON) != compute_fingerprint(WHEN, LAT, LON)


def test_fingerprint_changes_with_the_second() -> None:
    assert compute_fingerprint(WHEN + timedelta(seconds=1), LAT, LON) != compute_fingerprint(
        WHEN, LAT, LON
    )


def test_distance_km_matches_known_distances() -> None:
    jakarta = (Decimal("-6.2088"), Decimal("106.8456"))
    bogor = (Decimal("-6.5971"), Decimal("106.8060"))

    assert distance_km(*jakarta, *jakarta) == 0
    assert 42 < distance_km(*jakarta, *bogor) < 45  # PostGIS geography says ~43.4 km
    # 1 degree of latitude is ~111 km.
    assert distance_km(Decimal(0), Decimal(0), Decimal(1), Decimal(0)) == pytest.approx(111.2, 0.01)
