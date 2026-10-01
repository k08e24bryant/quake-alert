from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Earthquake
from app.ingestion.dedup import DedupConfig, UpsertOutcome, upsert_report
from app.ingestion.domain import Feed
from app.ingestion.parser import parse_feed
from tests.bmkg_samples import BASE_URL, DISTINCT_QUAKES, load, make_report

CONFIG = DedupConfig(max_time_diff=timedelta(seconds=60), max_distance_m=50_000)


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


async def test_new_report_is_inserted_with_all_fields(db_session: AsyncSession) -> None:
    report = make_report(felt="III Jayapura", shakemap_url="https://x/1.jpg")

    assert await upsert_report(db_session, report, CONFIG) is UpsertOutcome.INSERTED

    row = await only_row(db_session)
    assert row.occurred_at == report.occurred_at
    assert row.magnitude == Decimal("3.2")
    assert row.depth_km == 25
    assert await coordinates(db_session, row) == pytest.approx((-2.46, 140.38))
    assert row.region == report.region
    assert row.felt == "III Jayapura"
    assert row.shakemap_url == "https://x/1.jpg"
    assert row.source_feeds == ["autogempa"]
    assert row.fingerprint == report.fingerprint
    assert row.raw == {"autogempa": {"sample": True}}


async def test_same_quake_from_two_real_feeds_is_one_row_with_merged_fields(
    db_session: AsyncSession,
) -> None:
    [from_autogempa] = parse_feed(Feed.AUTOGEMPA, load(Feed.AUTOGEMPA), BASE_URL)
    from_dirasakan = parse_feed(Feed.GEMPADIRASAKAN, load(Feed.GEMPADIRASAKAN), BASE_URL)[0]
    assert from_dirasakan.fingerprint == from_autogempa.fingerprint  # sanity: same quake

    assert await upsert_report(db_session, from_dirasakan, CONFIG) is UpsertOutcome.INSERTED
    assert await upsert_report(db_session, from_autogempa, CONFIG) is UpsertOutcome.UPDATED

    row = await only_row(db_session)
    assert row.source_feeds == ["gempadirasakan", "autogempa"]
    assert row.felt == "II Kab. Jayapura"
    assert row.shakemap_url == "https://data.bmkg.go.id/DataMKG/TEWS/20261001132452.mmi.jpg"
    assert row.tsunami_potential == "Gempa ini dirasakan untuk diteruskan pada masyarakat"
    assert set(row.raw) == {"gempadirasakan", "autogempa"}


async def test_optional_fields_are_not_erased_by_a_feed_that_lacks_them(
    db_session: AsyncSession,
) -> None:
    with_extras = make_report(
        feed=Feed.AUTOGEMPA,
        felt="II Kab. Jayapura",
        shakemap_url="https://x/1.jpg",
        tsunami_potential="Tidak berpotensi tsunami",
    )
    bare = make_report(feed=Feed.GEMPATERKINI)

    await upsert_report(db_session, with_extras, CONFIG)
    await upsert_report(db_session, bare, CONFIG)

    row = await only_row(db_session)
    assert row.felt == "II Kab. Jayapura"
    assert row.shakemap_url == "https://x/1.jpg"
    assert row.tsunami_potential == "Tidak berpotensi tsunami"
    assert row.source_feeds == ["autogempa", "gempaterkini"]


async def test_near_duplicate_is_merged_and_takes_the_revised_values(
    db_session: AsyncSession,
) -> None:
    original = make_report(feed=Feed.AUTOGEMPA)
    revised = make_report(
        feed=Feed.GEMPATERKINI,
        occurred_at=original.occurred_at + timedelta(seconds=30),
        latitude=Decimal("-2.55"),  # ~10 km south
        longitude=Decimal("140.40"),
        magnitude=Decimal("3.4"),
        depth_km=18,
        region="revised region",
    )
    assert revised.fingerprint != original.fingerprint

    await upsert_report(db_session, original, CONFIG)
    assert await upsert_report(db_session, revised, CONFIG) is UpsertOutcome.UPDATED

    row = await only_row(db_session)
    assert row.occurred_at == revised.occurred_at
    assert row.magnitude == Decimal("3.4")
    assert row.depth_km == 18
    assert row.region == "revised region"
    assert await coordinates(db_session, row) == pytest.approx((-2.55, 140.40))
    assert row.fingerprint == original.fingerprint  # the unique key never moves
    assert row.source_feeds == ["autogempa", "gempaterkini"]


async def test_revised_magnitude_with_same_fingerprint_updates_the_row(
    db_session: AsyncSession,
) -> None:
    await upsert_report(db_session, make_report(magnitude=Decimal("5.0")), CONFIG)

    outcome = await upsert_report(db_session, make_report(magnitude=Decimal("5.3")), CONFIG)

    assert outcome is UpsertOutcome.UPDATED
    assert (await only_row(db_session)).magnitude == Decimal("5.3")


async def test_reprocessing_the_same_report_changes_nothing(db_session: AsyncSession) -> None:
    report = make_report()
    await upsert_report(db_session, report, CONFIG)
    await db_session.flush()
    updated_at = (await only_row(db_session)).updated_at

    assert await upsert_report(db_session, report, CONFIG) is UpsertOutcome.UNCHANGED
    await db_session.flush()

    assert (await only_row(db_session)).updated_at == updated_at


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("2 minutes apart, same place", {"seconds": 120}),
        ("same time, ~110 km apart", {"lat": "-3.46"}),
        ("61 s apart (just over the window)", {"seconds": 61}),
        ("~51 km apart (just over the radius)", {"lat": "-2.92"}),
    ],
)
async def test_distinct_quakes_are_separate_rows(
    db_session: AsyncSession, label: str, overrides: dict[str, Any]
) -> None:
    first = make_report()
    second = replace(
        first,
        feed=Feed.GEMPATERKINI,
        occurred_at=first.occurred_at + timedelta(seconds=overrides.get("seconds", 0)),
        latitude=Decimal(overrides.get("lat", str(first.latitude))),
    )

    await upsert_report(db_session, first, CONFIG)
    assert await upsert_report(db_session, second, CONFIG) is UpsertOutcome.INSERTED, label
    assert await count_rows(db_session) == 2


@pytest.mark.parametrize(
    ("label", "seconds", "lat"),
    [("exactly 60 s apart", 60, "-2.46"), ("~49 km apart", 0, "-2.90")],
)
async def test_reports_on_the_threshold_are_merged(
    db_session: AsyncSession, label: str, seconds: int, lat: str
) -> None:
    first = make_report()
    second = replace(
        first,
        feed=Feed.GEMPATERKINI,
        occurred_at=first.occurred_at + timedelta(seconds=seconds),
        latitude=Decimal(lat),
    )

    await upsert_report(db_session, first, CONFIG)
    assert await upsert_report(db_session, second, CONFIG) is UpsertOutcome.UPDATED, label
    assert await count_rows(db_session) == 1


async def test_thresholds_come_from_config(db_session: AsyncSession) -> None:
    strict = DedupConfig(max_time_diff=timedelta(seconds=10), max_distance_m=5_000)
    first = make_report()
    thirty_seconds_later = replace(first, occurred_at=first.occurred_at + timedelta(seconds=30))

    await upsert_report(db_session, first, strict)
    await upsert_report(db_session, thirty_seconds_later, strict)

    assert await count_rows(db_session) == 2


async def test_fuzzy_match_picks_the_closest_quake_in_time(db_session: AsyncSession) -> None:
    base = make_report()
    early = replace(base, occurred_at=base.occurred_at - timedelta(seconds=50))
    late = replace(base, occurred_at=base.occurred_at + timedelta(seconds=50), depth_km=99)
    # 100 s apart from each other: two separate rows.
    await upsert_report(db_session, early, CONFIG)
    await upsert_report(db_session, late, CONFIG)

    # Both are inside the 60 s window of the probe: early is 60 s away, late is 40 s away.
    probe = replace(base, occurred_at=base.occurred_at + timedelta(seconds=10), depth_km=7)
    await upsert_report(db_session, probe, CONFIG)

    depths = sorted((await db_session.scalars(select(Earthquake.depth_km))).all())
    assert depths == [7, 25]  # `late` (closest in time) was merged; `early` untouched


async def test_all_real_samples_dedupe_to_distinct_quakes(db_session: AsyncSession) -> None:
    for feed in Feed:
        for report in parse_feed(feed, load(feed), BASE_URL):
            await upsert_report(db_session, report, CONFIG)

    assert await count_rows(db_session) == DISTINCT_QUAKES
    merged = (
        await db_session.scalars(
            select(Earthquake).where(func.cardinality(Earthquake.source_feeds) > 1)
        )
    ).all()
    assert [sorted(row.source_feeds) for row in merged] == [["autogempa", "gempadirasakan"]]
