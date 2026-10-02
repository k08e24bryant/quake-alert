import json
import logging

import pytest

from app.core.logging import JsonFormatter, configure_logging, logging_config


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


def test_uvicorns_access_log_is_silenced() -> None:
    # It logs the full URL (query string included, which can hold a location) and the
    # client address; app.core.access_log replaces it.
    config = logging_config("DEBUG")["loggers"]["uvicorn.access"]

    assert config["handlers"] == []
    assert config["propagate"] is False
    assert config["level"] > logging.CRITICAL


def test_nothing_logged_on_uvicorns_access_logger_is_emitted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("DEBUG")
    logging.getLogger("uvicorn.access").critical(
        '%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", "/v1/earthquakes?lat=-6.2", "1.1", 200
    )
    logging.getLogger("app.access").info("request", extra={"path": "/v1/earthquakes"})

    out = capsys.readouterr().out
    assert "lat=-6.2" not in out
    assert "1.2.3.4" not in out
    assert '"path": "/v1/earthquakes"' in out  # our access log still works
