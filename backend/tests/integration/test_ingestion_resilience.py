import copy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Earthquake, IngestionRun, IngestionStatus
from app.ingestion.domain import Feed
from app.ingestion.service import FeedIngestionResult, ingest_feed_payload
from tests.bmkg_samples import BASE_URL, load

SETTINGS = Settings(bmkg_base_url=BASE_URL)

Factory = async_sessionmaker[AsyncSession]


async def ingest(session_factory: Factory, feed: Feed, payload: Any) -> FeedIngestionResult:
    return await ingest_feed_payload(session_factory, feed, payload, datetime.now(UTC), SETTINGS)


async def corrupt_earliest_terkini_payload(session_factory: Factory) -> Earthquake:
    """Make one stored gempaterkini payload unparseable, as if the parser got stricter."""
    async with session_factory() as session, session.begin():
        row = await session.scalar(select(Earthquake).order_by(Earthquake.occurred_at).limit(1))
        assert row is not None
        row.raw = {"gempaterkini": {**row.raw["gempaterkini"], "Kedalaman": "deep"}}
    return row


def as_dirasakan(terkini_payload: Any) -> Any:
    """The same quakes as gempadirasakan would publish them (felt info, no Potensi)."""
    items = []
    for item in terkini_payload["Infogempa"]["gempa"]:
        felt = {k: v for k, v in item.items() if k != "Potensi"}
        items.append({**felt, "Dirasakan": "III Kota"})
    return {"Infogempa": {"gempa": items}}


async def test_row_with_unparseable_stored_payload_is_skipped_not_fatal(
    session_factory: Factory,
) -> None:
    await ingest(session_factory, Feed.GEMPATERKINI, load(Feed.GEMPATERKINI))
    broken = await corrupt_earliest_terkini_payload(session_factory)

    # Another feed reports the same 15 quakes. Merging into the broken row needs its
    # gempaterkini payload re-parsed, which now fails.
    result = await ingest(
        session_factory, Feed.GEMPADIRASAKAN, as_dirasakan(load(Feed.GEMPATERKINI))
    )

    # The run carries on: 14 rows merged, the broken one skipped and counted.
    assert result.status is IngestionStatus.SUCCESS
    assert (result.inserted_count, result.updated_count, result.skipped_count) == (0, 14, 1)
    async with session_factory() as session:
        row = await session.get(Earthquake, broken.id)
        assert row is not None
        assert (row.raw, row.felt, row.source_feeds) == (broken.raw, None, ["gempaterkini"])
        run = await session.scalar(
            select(IngestionRun).order_by(IngestionRun.fetched_at.desc()).limit(1)
        )
        assert run is not None and run.skipped_count == 1


async def test_a_new_payload_from_the_same_feed_replaces_a_broken_one(
    session_factory: Factory,
) -> None:
    await ingest(session_factory, Feed.GEMPATERKINI, load(Feed.GEMPATERKINI))
    broken = await corrupt_earliest_terkini_payload(session_factory)

    revised = copy.deepcopy(load(Feed.GEMPATERKINI))
    for item in revised["Infogempa"]["gempa"]:
        item["Wilayah"] += " (revised)"
    result = await ingest(session_factory, Feed.GEMPATERKINI, revised)

    # Only the incoming feed's entry is replaced, so the broken payload is simply gone.
    assert (result.updated_count, result.skipped_count) == (15, 0)
    async with session_factory() as session:
        row = await session.get(Earthquake, broken.id)
        assert row is not None and row.region.endswith("(revised)")


async def test_aftershocks_in_one_real_snapshot_become_separate_rows(
    session_factory: Factory,
) -> None:
    snapshot = copy.deepcopy(load(Feed.GEMPATERKINI))
    items = snapshot["Infogempa"]["gempa"]
    main = items[0]  # 2026-09-21T23:48:13+00:00 at 4.74,125.30
    aftershock = {**main, "DateTime": "2026-09-21T23:48:43+00:00", "Coordinates": "4.83,125.30"}
    items.insert(1, aftershock)

    result = await ingest(session_factory, Feed.GEMPATERKINI, snapshot)

    assert result.inserted_count == 16
    async with session_factory() as session:
        count = len((await session.scalars(select(Earthquake.id))).all())
    assert count == 16
