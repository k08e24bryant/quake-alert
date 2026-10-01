"""Opaque keyset cursors for the (occurred_at DESC, id DESC) ordering.

A cursor names the last row of the previous page; the next page is every row strictly
after it in that ordering. Unlike offsets, rows inserted meanwhile never shift a page, and
rows sharing an occurred_at are split deterministically by id.
"""

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime


class InvalidCursorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Cursor:
    occurred_at: datetime
    id: uuid.UUID

    def encode(self) -> str:
        payload = json.dumps({"t": self.occurred_at.isoformat(), "id": str(self.id)})
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "Cursor":
        try:
            padded = value + "=" * (-len(value) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode()))
            occurred_at = datetime.fromisoformat(data["t"])
            row_id = uuid.UUID(data["id"])
        except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            raise InvalidCursorError("invalid cursor") from exc
        if occurred_at.tzinfo is None:
            raise InvalidCursorError("invalid cursor")
        return cls(occurred_at, row_id)
