from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Earthquake
from app.ingestion.dedup import DedupConfig, UpsertOutcome, upsert_report
from app.ingestion.domain import Feed, QuakeReport
from app.ingestion.parser import parse_feed
from tests.bmkg_samples import BASE_URL, DEFAULT_TIME, DISTINCT_QUAKES, load, make_report

CONFIG = DedupConfig(
    max_time_diff=timedelta(seconds=60), max_distance_m=50_000, shakemap_base_url=BASE_URL
)

DERIVED_AND_STORED = (
    "occurred_at",
    "magnitude",
    "depth_km",
    "region",
    "potential",
    "felt",
    "shakemap_url",
    "source_feeds",
    "fingerprint",
    "raw",
)


async def count_rows(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Earthquake)) or 0


async def only_row(session: AsyncSession) -> Earthquake:
    rows = (await session.scalars(select(Earthquake))).all()
    assert len(rows) == 1
    await session.refresh(rows[0])
    return rows[0]


async def coordinates(session: AsyncSession, row: Earthquake) -> tuple[float, float]:
    result = await session.execute(
        select(
            func.ST_Y(func.geometry(Earthquake.location)),
            func.ST_X(func.geometry(Earthquake.location)),
        ).where(Earthquake.id == row.id)
    )
    lat, lon = result.one()
    return lat, lon


async def snapshot(session: AsyncSession) -> dict[str, Any]:
    """Every stored column except id and timestamps, plus the coordinates."""
    row = await only_row(session)
    lat, lon = await coordinates(session, row)
    return {field: getattr(row, field) for field in DERIVED_AND_STORED} | {"lat": lat, "lon": lon}


async def upsert_all(session: AsyncSession, *reports: QuakeReport) -> list[UpsertOutcome]:
    return [await upsert_report(session, report, CONFIG) for report in reports]


async def reset(session: AsyncSession) -> None:
    await session.execute(delete(Earthquake))
    session.expunge_all()


# --- insert -------------------------------------------------------------------------------


async def test_new_report_is_inserted_with_all_fields(db_session: AsyncSession) -> None:
    report = make_report(
        Feed.AUTOGEMPA, felt="III Jayapura", potential="Tidak berpotensi tsunami", shakemap="1.jpg"
    )

    assert await upsert_report(db_session, report, CONFIG) is UpsertOutcome.INSERTED

    row = await only_row(db_session)
    assert row.occurred_at == DEFAULT_TIME
    assert row.magnitude == Decimal("3.2")
    assert row.depth_km == 25
    assert await coordinates(db_session, row) == pytest.approx((-2.46, 140.38))
    assert row.region == report.region
    assert row.felt == "III Jayapura"
    assert row.potential == "Tidak berpotensi tsunami"
    assert row.shakemap_url == f"{BASE_URL}1.jpg"
    assert row.source_feeds == ["autogempa"]
    assert row.fingerprint == report.fingerprint
    assert row.raw == {"autogempa": report.raw}


# --- merge: source precedence --------------------------------------------------------------


async def test_same_quake_from_two_real_feeds_is_one_row_with_merged_fields(
    db_session: AsyncSession,
) -> None:
    [from_autogempa] = parse_feed(Feed.AUTOGEMPA, load(Feed.AUTOGEMPA), BASE_URL).reports
    from_dirasakan = parse_feed(Feed.GEMPADIRASAKAN, load(Feed.GEMPADIRASAKAN), BASE_URL).reports[0]
    assert from_dirasakan.fingerprint == from_autogempa.fingerprint  # sanity: same quake

    assert await upsert_all(db_session, from_dirasakan, from_autogempa) == [
        UpsertOutcome.INSERTED,
        UpsertOutcome.UPDATED,
    ]

    row = await only_row(db_session)
    assert row.source_feeds == ["autogempa", "gempadirasakan"]  # precedence order
    assert row.felt == "II Kab. Jayapura"
    assert row.shakemap_url == "https://data.bmkg.go.id/DataMKG/TEWS/20261001132452.mmi.jpg"
    assert row.potential == "Gempa ini dirasakan untuk diteruskan pada masyarakat"
    assert row.raw == {"autogempa": from_autogempa.raw, "gempadirasakan": from_dirasakan.raw}


def conflicting_pair() -> tuple[QuakeReport, QuakeReport]:
    """The same quake (same fingerprint) as two feeds report it, disagreeing on everything
    that can differ."""
    autogempa = make_report(
        Feed.AUTOGEMPA, magnitude="5.3", depth_km=12, region="A region", shakemap="a.jpg"
    )
    dirasakan = make_report(
        Feed.GEMPADIRASAKAN, magnitude="5.1", depth_km=30, region="D region", felt="IV Kota D"
    )
    return autogempa, dirasakan


async def test_core_fields_come_from_the_highest_precedence_feed(
    db_session: AsyncSession,
) -> None:
    autogempa, dirasakan = conflicting_pair()

    await upsert_all(db_session, autogempa, dirasakan)

    row = await only_row(db_session)
    assert (row.magnitude, row.depth_km, row.region) == (Decimal("5.3"), 12, "A region")
    # felt/shakemap: from the highest-precedence feed that has one.
    assert row.felt == "IV Kota D"
    assert row.shakemap_url == f"{BASE_URL}a.jpg"


async def test_conflicting_feeds_give_identical_rows_in_either_order(
    db_session: AsyncSession,
) -> None:
    autogempa, dirasakan = conflicting_pair()

    await upsert_all(db_session, autogempa, dirasakan)
    forward = await snapshot(db_session)
    await reset(db_session)
    await upsert_all(db_session, dirasakan, autogempa)
    backward = await snapshot(db_session)

    assert forward == backward


async def test_near_duplicates_give_the_same_row_in_either_order_except_fingerprint(
    db_session: AsyncSession,
) -> None:
    terkini = make_report(Feed.GEMPATERKINI, magnitude="5.6", potential="Tidak berpotensi tsunami")
    dirasakan = make_report(
        Feed.GEMPADIRASAKAN,
        occurred_at=DEFAULT_TIME + timedelta(seconds=30),
        latitude="-2.55",  # ~10 km away
        magnitude="5.4",
        felt="IV Kota D",
    )
    assert terkini.fingerprint != dirasakan.fingerprint

    await upsert_all(db_session, terkini, dirasakan)
    forward = await snapshot(db_session)
    await reset(db_session)
    await upsert_all(db_session, dirasakan, terkini)
    backward = await snapshot(db_session)

    # The fingerprint is immutable, so it is whichever report created the row ...
    assert forward.pop("fingerprint") == terkini.fingerprint
    assert backward.pop("fingerprint") == dirasakan.fingerprint
    # ... but everything derived is identical, taken from gempaterkini.
    assert forward == backward
    assert forward["magnitude"] == Decimal("5.6")
    assert (forward["lat"], forward["lon"]) == pytest.approx((-2.46, 140.38))
    assert forward["occurred_at"] == DEFAULT_TIME
    assert forward["felt"] == "IV Kota D"
    assert forward["potential"] == "Tidak berpotensi tsunami"


async def test_repeated_alternating_polls_never_change_the_row(db_session: AsyncSession) -> None:
    autogempa, dirasakan = conflicting_pair()
    await upsert_all(db_session, autogempa, dirasakan)
    await db_session.flush()
    settled = await snapshot(db_session)
    updated_at = (await only_row(db_session)).updated_at

    outcomes = await upsert_all(db_session, dirasakan, autogempa, dirasakan, autogempa, dirasakan)
    await db_session.flush()

    assert outcomes == [UpsertOutcome.UNCHANGED] * 5
    assert await snapshot(db_session) == settled
    assert (await only_row(db_session)).updated_at == updated_at


async def test_revision_within_the_same_feed_is_applied(db_session: AsyncSession) -> None:
    await upsert_report(db_session, make_report(Feed.AUTOGEMPA, magnitude="5.0"), CONFIG)

    revised = make_report(Feed.AUTOGEMPA, magnitude="5.3", depth_km=40)
    assert await upsert_report(db_session, revised, CONFIG) is UpsertOutcome.UPDATED

    row = await only_row(db_session)
    assert (row.magnitude, row.depth_km) == (Decimal("5.3"), 40)
    assert row.raw == {"autogempa": revised.raw}


async def test_revision_in_a_lower_feed_is_stored_but_does_not_override(
    db_session: AsyncSession,
) -> None:
    autogempa, dirasakan = conflicting_pair()
    await upsert_all(db_session, autogempa, dirasakan)

    revised = make_report(Feed.GEMPADIRASAKAN, magnitude="4.9", felt="V Kota D")
    assert await upsert_report(db_session, revised, CONFIG) is UpsertOutcome.UPDATED

    row = await only_row(db_session)
    assert row.magnitude == Decimal("5.3")  # still autogempa's
    assert row.felt == "V Kota D"  # only dirasakan has felt, so its revision shows
    assert row.raw["gempadirasakan"] == revised.raw


async def test_optional_fields_survive_a_feed_that_lacks_them(db_session: AsyncSession) -> None:
    with_extras = make_report(
        Feed.AUTOGEMPA, felt="II Kab. Jayapura", shakemap="1.jpg", potential="Tidak berpotensi"
    )
    bare = make_report(Feed.GEMPATERKINI)

    await upsert_all(db_session, with_extras, bare)

    row = await only_row(db_session)
    assert (row.felt, row.shakemap_url, row.potential) == (
        "II Kab. Jayapura",
        f"{BASE_URL}1.jpg",
        "Tidak berpotensi",
    )
    assert row.source_feeds == ["autogempa", "gempaterkini"]


# --- matching -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "seconds", "lat"),
    [
        ("2 minutes apart, same place", 120, "-2.46"),
        ("same time, ~110 km apart", 0, "-3.46"),
        ("61 s apart (just over the window)", 61, "-2.46"),
        ("~51 km apart (just over the radius)", 0, "-2.92"),
    ],
)
async def test_distinct_quakes_are_separate_rows(
    db_session: AsyncSession, label: str, seconds: int, lat: str
) -> None:
    second = make_report(
        Feed.GEMPATERKINI, occurred_at=DEFAULT_TIME + timedelta(seconds=seconds), latitude=lat
    )

    outcomes = await upsert_all(db_session, make_report(), second)

    assert outcomes == [UpsertOutcome.INSERTED, UpsertOutcome.INSERTED], label
    assert await count_rows(db_session) == 2


@pytest.mark.parametrize(
    ("label", "seconds", "lat"),
    [("exactly 60 s apart", 60, "-2.46"), ("~49 km apart", 0, "-2.90")],
)
async def test_reports_on_the_threshold_are_merged(
    db_session: AsyncSession, label: str, seconds: int, lat: str
) -> None:
    second = make_report(
        Feed.GEMPATERKINI, occurred_at=DEFAULT_TIME + timedelta(seconds=seconds), latitude=lat
    )

    outcomes = await upsert_all(db_session, make_report(), second)

    assert outcomes == [UpsertOutcome.INSERTED, UpsertOutcome.UPDATED], label
    assert await count_rows(db_session) == 1


async def test_thresholds_come_from_config(db_session: AsyncSession) -> None:
    strict = DedupConfig(
        max_time_diff=timedelta(seconds=10), max_distance_m=5_000, shakemap_base_url=BASE_URL
    )
    later = make_report(Feed.GEMPATERKINI, occurred_at=DEFAULT_TIME + timedelta(seconds=30))

    await upsert_report(db_session, make_report(), strict)
    await upsert_report(db_session, later, strict)

    assert await count_rows(db_session) == 2


async def test_fuzzy_match_picks_the_closest_quake_in_time(db_session: AsyncSession) -> None:
    # 100 s apart from each other: two separate rows.
    early = make_report(occurred_at=DEFAULT_TIME - timedelta(seconds=50), depth_km=25)
    late = make_report(occurred_at=DEFAULT_TIME + timedelta(seconds=50), depth_km=99)
    await upsert_all(db_session, early, late)

    # Both are inside the probe's 60 s window: early is 60 s away, late is 40 s away.
    probe = make_report(
        Feed.GEMPATERKINI, occurred_at=DEFAULT_TIME + timedelta(seconds=10), felt="probe"
    )
    await upsert_report(db_session, probe, CONFIG)

    rows = (await db_session.scalars(select(Earthquake).order_by(Earthquake.occurred_at))).all()
    assert [r.source_feeds for r in rows] == [["autogempa"], ["autogempa", "gempaterkini"]]


async def test_all_real_samples_dedupe_to_distinct_quakes(db_session: AsyncSession) -> None:
    for feed in Feed:
        await upsert_all(db_session, *parse_feed(feed, load(feed), BASE_URL).reports)

    assert await count_rows(db_session) == DISTINCT_QUAKES
    merged = (
        await db_session.scalars(
            select(Earthquake).where(func.cardinality(Earthquake.source_feeds) > 1)
        )
    ).all()
    assert [row.source_feeds for row in merged] == [["autogempa", "gempadirasakan"]]
