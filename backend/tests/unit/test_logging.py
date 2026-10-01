import json
import logging

from app.core.logging import JsonFormatter


def test_json_formatter_emits_one_json_object_with_extra_fields() -> None:
    record = logging.makeLogRecord(
        {
            "name": "quake",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "ingested %d quakes",
            "args": (3,),
            "feed": "autogempa",
        }
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "ingested 3 quakes"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "quake"
    assert payload["feed"] == "autogempa"
    assert payload["timestamp"].endswith("+00:00")
    assert "args" not in payload
