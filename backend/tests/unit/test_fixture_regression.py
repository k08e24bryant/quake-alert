"""Stored raw payloads are re-parsed on every merge, so the parser must keep accepting
everything BMKG has ever sent us. Every saved response must still parse cleanly.

Name new fixtures `<feed>.json` or `<feed>-<suffix>.json`.
"""

import json
from pathlib import Path

import pytest

from app.ingestion.domain import Feed
from app.ingestion.parser import parse_feed
from tests.bmkg_samples import BASE_URL, FIXTURES_DIR

FIXTURES = sorted(FIXTURES_DIR.glob("*.json"))


def feed_of(path: Path) -> Feed:
    return next(f for f in Feed if path.stem == f.value or path.stem.startswith(f"{f.value}-"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.name)
def test_every_saved_fixture_still_parses_cleanly(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))

    parsed = parse_feed(feed_of(path), payload, BASE_URL)

    assert parsed.reports
    assert parsed.skipped_count == 0


def test_there_is_a_fixture_for_every_feed() -> None:
    assert {feed_of(path) for path in FIXTURES} == set(Feed)
