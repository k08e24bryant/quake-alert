import json
import logging
import logging.config
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord has; anything else was passed via `extra=` and is emitted as a field.
_RESERVED_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

# Libraries that install their own plain-text handlers; route them through ours instead.
_THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "arq")

# Above CRITICAL: nothing logged here is ever emitted.
_OFF = logging.CRITICAL + 10


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def logging_config(level: str) -> dict[str, Any]:
    """A logging.config.dictConfig dict: everything goes to stdout as one JSON object per line."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": JsonFormatter}},
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "json",
            }
        },
        "root": {"level": level, "handlers": ["stdout"]},
        "loggers": {
            **{
                name: {"handlers": [], "propagate": True, "level": level}
                for name in _THIRD_PARTY_LOGGERS
            },
            # uvicorn's access log has the full URL (query string included, which can hold
            # a visitor's location) and the client address. app.core.access_log replaces it.
            "uvicorn.access": {"handlers": [], "propagate": False, "level": _OFF},
            # httpx logs every request at INFO; with a poll per feed per minute that is noise.
            "httpx": {"level": "WARNING"},
        },
    }


def configure_logging(level: str) -> None:
    logging.config.dictConfig(logging_config(level))
