"""Our access log: method, path, status, duration, and never a query string, so a
visitor's location (lat/lon from "Gempa di sekitar saya") is never written to a log."""

import json
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import JsonFormatter
from tests.integration.seed import JAKARTA, seed_quake

LOCATION_QUERY = "lat=-6.2088&lon=106.8456&radius_km=300&min_mag=4"
SECRET_BITS = ("-6.2088", "106.8456", "lat", "lon", "radius_km", "min_mag", "?")


def access_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.access"]


def every_line(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every record, of every logger, as it would be written (our JSON formatter)."""
    formatter = JsonFormatter()
    return [formatter.format(record) for record in caplog.records]


async def test_location_query_is_never_logged(
    api: AsyncClient, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await seed_quake(db_session, at=JAKARTA)
    caplog.set_level(logging.DEBUG)

    response = await api.get(f"/v1/earthquakes?{LOCATION_QUERY}")

    assert response.status_code == 200
    [record] = access_records(caplog)
    line = json.loads(JsonFormatter().format(record))
    assert {k: line[k] for k in ("logger", "message", "method", "path", "status")} == {
        "logger": "app.access",
        "message": "request",
        "method": "GET",
        "path": "/v1/earthquakes",
        "status": 200,
    }
    assert isinstance(line["duration_ms"], float) and line["duration_ms"] >= 0
    # Only those fields: no query, no client address, no headers.
    assert set(line) == {
        "timestamp", "level", "logger", "message", "method", "path", "status", "duration_ms",
    }  # fmt: skip
    for written in every_line(caplog):
        for bit in SECRET_BITS:
            assert bit not in written, (bit, written)


async def test_rejected_requests_are_logged_without_their_query(
    api: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    response = await api.get("/v1/earthquakes?lat=-6.2088")  # lon missing: 422

    assert response.status_code == 422
    [record] = access_records(caplog)
    assert (record.path, record.status) == ("/v1/earthquakes", 422)  # type: ignore[attr-defined]
    assert all("-6.2088" not in written for written in every_line(caplog))


async def test_a_crash_is_logged_as_500(api_app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
    async def boom() -> None:
        raise RuntimeError("boom")

    api_app.add_api_route("/test-boom", boom)
    caplog.set_level(logging.DEBUG)
    transport = ASGITransport(app=api_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/test-boom?lat=-6.2088")

    assert response.status_code == 500
    [record] = access_records(caplog)
    assert (record.path, record.status) == ("/test-boom", 500)  # type: ignore[attr-defined]
