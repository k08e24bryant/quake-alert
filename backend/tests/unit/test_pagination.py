import base64
import uuid
from datetime import UTC, datetime

import pytest

from app.earthquakes.pagination import Cursor, InvalidCursorError


def test_cursor_round_trips() -> None:
    cursor = Cursor(datetime(2026, 10, 1, 6, 24, 52, 123456, tzinfo=UTC), uuid.uuid4())

    assert Cursor.decode(cursor.encode()) == cursor
    assert "=" not in cursor.encode()  # URL-safe without padding


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not base64!",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(b'{"t": "2026-10-01T00:00:00+00:00"}').decode(),
        base64.urlsafe_b64encode(b'{"t": "yesterday", "id": "x"}').decode(),
        # Naive timestamps would compare wrongly against timestamptz.
        base64.urlsafe_b64encode(
            f'{{"t": "2026-10-01T00:00:00", "id": "{uuid.uuid4()}"}}'.encode()
        ).decode(),
    ],
)
def test_invalid_cursors_are_rejected(value: str) -> None:
    with pytest.raises(InvalidCursorError):
        Cursor.decode(value)
