import copy
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.ingestion.domain import Feed
from app.ingestion.parser import (
    BmkgParseError,
    parse_coordinates,
    parse_datetime,
    parse_depth_km,
    parse_feed,
    parse_magnitude,
)
from tests.bmkg_samples import BASE_URL, load


def test_autogempa_single_object_is_parsed_with_every_field() -> None:
    [report] = parse_feed(Feed.AUTOGEMPA, load(Feed.AUTOGEMPA), BASE_URL).reports

    assert report.feed is Feed.AUTOGEMPA
    assert report.occurred_at == datetime(2026, 10, 1, 6, 24, 52, tzinfo=UTC)
    assert report.magnitude == Decimal("3.2")
    assert report.depth_km == 25
    assert (report.latitude, report.longitude) == (Decimal("-2.46"), Decimal("140.38"))
    assert report.region == "Pusat gempa berada di darat 15 km Barat Laut Sentani"
    assert report.potential == "Gempa ini dirasakan untuk diteruskan pada masyarakat"
    assert report.felt == "II Kab. Jayapura"
    assert report.shakemap_url == ("https://data.bmkg.go.id/DataMKG/TEWS/20261001132452.mmi.jpg")
    assert report.raw == load(Feed.AUTOGEMPA)["Infogempa"]["gempa"]


def test_gempaterkini_list_has_potential_but_no_felt_or_shakemap() -> None:
    reports = parse_feed(Feed.GEMPATERKINI, load(Feed.GEMPATERKINI), BASE_URL).reports

    assert len(reports) == 15
    first = reports[0]
    assert first.occurred_at == datetime(2026, 9, 21, 23, 48, 13, tzinfo=UTC)
    assert first.magnitude == Decimal("5.2")
    assert first.depth_km == 10
    assert (first.latitude, first.longitude) == (Decimal("4.74"), Decimal("125.30"))
    assert first.region == "127 km BaratLaut TAHUNA-KEP.SANGIHE-SULUT"
    assert first.potential == "Tidak berpotensi tsunami"
    assert all(r.felt is None and r.shakemap_url is None for r in reports)
    assert all(r.magnitude >= 5 for r in reports)  # the feed is M5+ only


def test_gempadirasakan_list_has_felt_but_no_potential() -> None:
    reports = parse_feed(Feed.GEMPADIRASAKAN, load(Feed.GEMPADIRASAKAN), BASE_URL).reports

    assert len(reports) == 15
    last = reports[-1]
    assert last.occurred_at == datetime(2026, 9, 25, 8, 4, 42, tzinfo=UTC)
    assert last.magnitude == Decimal("2.4")
    assert last.depth_km == 7
    assert (last.latitude, last.longitude) == (Decimal("-6.90"), Decimal("107.11"))
    assert last.felt == "II - III Kota Cianjur, II - III Cibeber, II - III Warungkondang"
    assert all(r.potential is None for r in reports)


@pytest.mark.parametrize("feed", list(Feed))
def test_every_sample_item_parses_to_utc(feed: Feed) -> None:
    payload = load(feed)
    items = payload["Infogempa"]["gempa"]
    expected = 1 if isinstance(items, dict) else len(items)

    parsed = parse_feed(feed, payload, BASE_URL)
    reports = parsed.reports

    assert len(reports) == expected
    assert parsed.skipped_count == 0
    assert all(r.occurred_at.utcoffset() is not None for r in reports)
    assert all(r.occurred_at.tzinfo is UTC for r in reports)


def test_local_tanggal_and_jam_are_ignored_in_favour_of_utc_datetime() -> None:
    payload = copy.deepcopy(load(Feed.AUTOGEMPA))
    payload["Infogempa"]["gempa"]["Jam"] = "23:59:59 WIB"

    [report] = parse_feed(Feed.AUTOGEMPA, payload, BASE_URL).reports

    assert report.occurred_at == datetime(2026, 10, 1, 6, 24, 52, tzinfo=UTC)


def test_malformed_item_is_skipped_and_the_rest_are_kept() -> None:
    payload = copy.deepcopy(load(Feed.GEMPATERKINI))
    payload["Infogempa"]["gempa"][3]["Magnitude"] = "not a number"
    del payload["Infogempa"]["gempa"][7]["Coordinates"]

    parsed = parse_feed(Feed.GEMPATERKINI, payload, BASE_URL)

    assert len(parsed.reports) == 13
    assert parsed.skipped_count == 2


@pytest.mark.parametrize(
    "payload",
    [{}, {"Infogempa": {}}, {"Infogempa": {"gempa": "oops"}}, [], None],
)
def test_wrong_envelope_raises(payload: object) -> None:
    with pytest.raises(BmkgParseError):
        parse_feed(Feed.AUTOGEMPA, payload, BASE_URL)


def test_blank_optional_fields_become_none() -> None:
    payload = copy.deepcopy(load(Feed.AUTOGEMPA))
    payload["Infogempa"]["gempa"].update({"Dirasakan": " ", "Shakemap": ""})

    [report] = parse_feed(Feed.AUTOGEMPA, payload, BASE_URL).reports

    assert report.felt is None
    assert report.shakemap_url is None


def test_datetime_with_non_utc_offset_is_converted_to_utc() -> None:
    assert parse_datetime("2026-10-01T13:24:52+07:00") == datetime(
        2026, 10, 1, 6, 24, 52, tzinfo=UTC
    )


@pytest.mark.parametrize("value", ["2026-10-01T06:24:52", "01 Okt 2026", ""])
def test_datetime_without_offset_or_garbage_is_rejected(value: str) -> None:
    with pytest.raises(BmkgParseError):
        parse_datetime(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-2.46,140.38", (Decimal("-2.46"), Decimal("140.38"))),
        ("4.74, 125.30", (Decimal("4.74"), Decimal("125.30"))),
    ],
)
def test_coordinates_are_lat_then_lon(value: str, expected: tuple[Decimal, Decimal]) -> None:
    assert parse_coordinates(value) == expected


@pytest.mark.parametrize("value", ["-2.46", "1,2,3", "abc,def", "91,0", "0,181", "NaN,0"])
def test_invalid_coordinates_are_rejected(value: str) -> None:
    with pytest.raises(BmkgParseError):
        parse_coordinates(value)


@pytest.mark.parametrize(("value", "expected"), [("5.2", "5.2"), (" 4 ", "4.0"), ("5.20", "5.2")])
def test_magnitude_is_a_one_decimal_number(value: str, expected: str) -> None:
    assert parse_magnitude(value) == Decimal(expected)


# "3.25" would have to be rounded to fit numeric(3,1): rejected (the item is skipped and
# logged), because BMKG's numbers are relayed as-is, never modified.
@pytest.mark.parametrize("value", ["", "M5.2", "-1", "10.0", "3.25", "5.21"])
def test_invalid_magnitude_is_rejected(value: str) -> None:
    with pytest.raises(BmkgParseError):
        parse_magnitude(value)


@pytest.mark.parametrize(("value", "expected"), [("10 km", 10), ("7 km", 7), ("135 KM", 135)])
def test_depth_drops_the_km_suffix(value: str, expected: int) -> None:
    assert parse_depth_km(value) == expected


@pytest.mark.parametrize("value", ["10", "10.5 km", "km", "-3 km"])
def test_invalid_depth_is_rejected(value: str) -> None:
    with pytest.raises(BmkgParseError):
        parse_depth_km(value)
