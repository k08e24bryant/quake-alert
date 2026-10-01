import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import IngestionStatus
from app.ingestion.domain import Feed
from app.ingestion.freshness import (
    FeedFreshness,
    IngestionFreshness,
    IngestionState,
    StalenessMonitor,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
STALE_AFTER = timedelta(minutes=5)


def freshness(**minutes_since_success: float | None) -> IngestionFreshness:
    """Per feed: minutes since its last successful run (None = never succeeded)."""
    feeds = []
    for feed in Feed:
        minutes = minutes_since_success.get(feed.value, 1)
        success_at = None if minutes is None else NOW - timedelta(minutes=minutes)
        feeds.append(
            FeedFreshness(
                feed=feed,
                last_run_at=success_at,
                last_run_status=IngestionStatus.SUCCESS if success_at else None,
                last_success_at=success_at,
            )
        )
    return IngestionFreshness(feeds=feeds, checked_at=NOW, stale_after=STALE_AFTER)


def test_ok_when_every_feed_succeeded_recently() -> None:
    result = freshness()

    assert result.state is IngestionState.OK
    assert result.stale_feeds == []
    assert result.data_as_of == NOW - timedelta(minutes=1)


def test_stale_after_window_is_inclusive_boundary() -> None:
    assert freshness(autogempa=5).state is IngestionState.OK
    assert freshness(autogempa=5.01).state is IngestionState.STALE


def test_one_stale_feed_makes_ingestion_stale() -> None:
    result = freshness(gempaterkini=12)

    assert result.state is IngestionState.STALE
    assert result.stale_feeds == [Feed.GEMPATERKINI]
    # data_as_of is the latest success across feeds, so one stale feed doesn't move it.
    assert result.data_as_of == NOW - timedelta(minutes=1)


def test_never_run_feed_is_stale() -> None:
    result = freshness(gempadirasakan=None)

    assert result.state is IngestionState.STALE
    assert result.stale_feeds == [Feed.GEMPADIRASAKAN]


def test_nothing_ever_ran() -> None:
    result = freshness(autogempa=None, gempaterkini=None, gempadirasakan=None)

    assert result.state is IngestionState.STALE
    assert result.data_as_of is None


def _transitions(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == "app.ingestion.freshness"
    ]


def test_monitor_logs_once_when_going_stale_and_once_on_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor = StalenessMonitor()
    caplog.set_level(logging.INFO, logger="app.ingestion.freshness")

    for state in (freshness(), freshness(autogempa=9), freshness(autogempa=10), freshness()):
        monitor.observe(state)
    monitor.observe(freshness())

    assert _transitions(caplog) == [
        ("WARNING", "BMKG ingestion is stale"),
        ("INFO", "BMKG ingestion recovered"),
    ]
    stale_record = next(r for r in caplog.records if r.levelname == "WARNING")
    assert stale_record.__dict__["stale_feeds"] == ["autogempa"]


def test_monitor_starting_fresh_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StalenessMonitor()
    caplog.set_level(logging.INFO, logger="app.ingestion.freshness")

    monitor.observe(freshness())
    monitor.observe(freshness())

    assert _transitions(caplog) == []
    assert monitor.state is IngestionState.OK


def test_monitor_starting_stale_logs_it_once(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StalenessMonitor()
    caplog.set_level(logging.INFO, logger="app.ingestion.freshness")

    monitor.observe(freshness(gempaterkini=None))
    monitor.observe(freshness(gempaterkini=None))

    assert _transitions(caplog) == [("WARNING", "BMKG ingestion is stale")]
