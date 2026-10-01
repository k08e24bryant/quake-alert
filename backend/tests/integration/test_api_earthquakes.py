import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.domain import distance_km
from tests.bmkg_samples import DEFAULT_TIME
from tests.integration.seed import BANDUNG, BOGOR, JAKARTA, SURABAYA, seed_quake

SOURCE = {
    "name": "BMKG (Badan Meteorologi, Klimatologi, dan Geofisika)",
    "url": "https://data.bmkg.go.id/",
    "notice": "Sumber: BMKG",
    "data_as_of": None,  # no ingestion_runs in these tests; see test_status.py
}


def regions(body: dict[str, Any]) -> list[str]:
    return [quake["region"] for quake in body["data"]]


async def seed_cities(session: AsyncSession) -> None:
    # Newest first: Jakarta, Bogor, Bandung, Surabaya.
    for minutes, (name, at) in enumerate(
        [("Jakarta", JAKARTA), ("Bogor", BOGOR), ("Bandung", BANDUNG), ("Surabaya", SURABAYA)]
    ):
        await seed_quake(
            session, at=at, region=name, occurred_at=DEFAULT_TIME - timedelta(minutes=minutes)
        )


# --- geospatial ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("radius_km", "expected"),
    [
        (10, ["Jakarta"]),
        (50, ["Jakarta", "Bogor"]),  # Bogor ~43 km
        (150, ["Jakarta", "Bogor", "Bandung"]),  # Bandung ~116 km
        (1000, ["Jakarta", "Bogor", "Bandung", "Surabaya"]),  # Surabaya ~663 km
    ],
)
async def test_radius_filter_with_known_city_distances(
    api: AsyncClient, db_session: AsyncSession, radius_km: int, expected: list[str]
) -> None:
    await seed_cities(db_session)
    lat, lon = JAKARTA

    response = await api.get(
        "/v1/earthquakes", params={"lat": lat, "lon": lon, "radius_km": radius_km}
    )

    assert response.status_code == 200
    assert regions(response.json()) == expected


async def test_distance_km_is_geodesic_and_matches_known_values(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_cities(db_session)
    lat, lon = JAKARTA

    body = (await api.get("/v1/earthquakes", params={"lat": lat, "lon": lon})).json()

    distances = {quake["region"]: quake["distance_km"] for quake in body["data"]}
    assert distances["Jakarta"] == pytest.approx(0, abs=0.001)
    assert distances["Bogor"] == pytest.approx(43.16, abs=0.05)
    assert distances["Bandung"] == pytest.approx(116.02, abs=0.05)
    assert distances["Surabaya"] == pytest.approx(663.21, abs=0.05)
    # Independent cross-check: spherical haversine is within ~0.6% of the spheroid.
    for city, at in {"Bogor": BOGOR, "Bandung": BANDUNG, "Surabaya": SURABAYA}.items():
        sphere = distance_km(*map(Decimal, map(str, (lat, lon, *at))))
        assert distances[city] == pytest.approx(sphere, rel=0.01), city


async def test_radius_boundary_is_inclusive_of_closer_and_exclusive_of_farther(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_cities(db_session)
    lat, lon = JAKARTA
    bogor_km = (await api.get("/v1/earthquakes", params={"lat": lat, "lon": lon})).json()
    exact = next(q["distance_km"] for q in bogor_km["data"] if q["region"] == "Bogor")

    just_inside = await api.get(
        "/v1/earthquakes", params={"lat": lat, "lon": lon, "radius_km": exact + 0.01}
    )
    just_outside = await api.get(
        "/v1/earthquakes", params={"lat": lat, "lon": lon, "radius_km": exact - 0.01}
    )

    assert "Bogor" in regions(just_inside.json())
    assert "Bogor" not in regions(just_outside.json())


async def test_coordinates_are_returned_as_latitude_and_longitude(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_quake(db_session, at=SURABAYA)

    [quake] = (await api.get("/v1/earthquakes")).json()["data"]

    assert (quake["latitude"], quake["longitude"]) == pytest.approx(SURABAYA)
    assert quake["distance_km"] is None  # no lat/lon given


# --- other filters ------------------------------------------------------------------------


async def test_magnitude_and_time_filters(api: AsyncClient, db_session: AsyncSession) -> None:
    for i, magnitude in enumerate(["3.0", "4.5", "5.0", "6.2"]):
        await seed_quake(
            db_session,
            magnitude=magnitude,
            region=magnitude,
            occurred_at=DEFAULT_TIME - timedelta(days=i),
        )

    by_magnitude = await api.get("/v1/earthquakes", params={"min_mag": 4.5, "max_mag": 5.0})
    by_time = await api.get(
        "/v1/earthquakes",
        params={
            "start": (DEFAULT_TIME - timedelta(days=2)).isoformat(),
            "end": DEFAULT_TIME.isoformat(),  # exclusive
        },
    )

    assert regions(by_magnitude.json()) == ["4.5", "5.0"]  # newest first
    assert regions(by_time.json()) == ["4.5", "5.0"]


# --- response shape -----------------------------------------------------------------------


async def test_response_shape_hides_internal_fields_and_attributes_bmkg(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    row = await seed_quake(
        db_session, potential="Gempa ini dirasakan untuk diteruskan pada masyarakat", felt="III"
    )

    listed = (await api.get("/v1/earthquakes")).json()
    latest = (await api.get("/v1/earthquakes/latest")).json()
    detail = (await api.get(f"/v1/earthquakes/{row.id}")).json()

    for body in (listed, latest, detail):
        assert body["source"] == SOURCE
    quake = detail["data"]
    assert set(quake) == {
        "id", "occurred_at", "magnitude", "depth_km", "latitude", "longitude", "region",
        "potential", "felt", "shakemap_url", "source_feeds", "distance_km",
    }  # fmt: skip
    assert "raw" not in quake and "fingerprint" not in quake
    assert quake["potential"] == "Gempa ini dirasakan untuk diteruskan pada masyarakat"
    assert quake["source_feeds"] == ["gempaterkini"]
    assert quake["occurred_at"] == "2026-10-01T06:24:52+00:00"  # ISO 8601 with offset
    assert quake["magnitude"] == 5.0
    assert listed["data"] == [quake] and latest["data"] == quake


async def test_openapi_describes_potential_as_not_tsunami_information(api: AsyncClient) -> None:
    schema = (await api.get("/openapi.json")).json()

    properties = schema["components"]["schemas"]["Earthquake"]["properties"]
    description = properties["potential"]["description"]
    assert '"Potensi"' in description
    assert "NOT tsunami information" in description
    assert "raw" not in properties and "fingerprint" not in properties


async def test_detail_404_and_malformed_id(api: AsyncClient) -> None:
    assert (await api.get(f"/v1/earthquakes/{uuid.uuid4()}")).status_code == 404
    assert (await api.get("/v1/earthquakes/not-a-uuid")).status_code == 422


async def test_latest_404_when_empty(api: AsyncClient) -> None:
    assert (await api.get("/v1/earthquakes/latest")).status_code == 404


# --- validation ---------------------------------------------------------------------------


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"radius_km": 10}, "radius_km requires lat and lon"),
        ({"lat": -6.2}, "lat and lon must be given together"),
        ({"lon": 106.8}, "lat and lon must be given together"),
        ({"lat": -6.2, "lon": 106.8, "radius_km": 1001}, "less than or equal to 1000"),
        ({"lat": -6.2, "lon": 106.8, "radius_km": 0}, "greater than 0"),
        ({"lat": 91, "lon": 0}, "less than or equal to 90"),
        ({"lat": 0, "lon": 181}, "less than or equal to 180"),
        ({"min_mag": 6, "max_mag": 5}, "min_mag must not be greater than max_mag"),
        ({"limit": 101}, "less than or equal to 100"),
        ({"limit": 0}, "greater than or equal to 1"),
        ({"start": _days_ago(1), "end": _days_ago(2)}, "end must be after start"),
        ({"start": _days_ago(400), "end": _days_ago(33)}, "at most 366 days"),
        ({"start": _days_ago(367)}, "at most 366 days"),
        ({"start": "2026-10-01T00:00:00"}, "timezone"),
        ({"cursor": "garbage"}, "cursor is invalid"),
        ({"magnitude": 5}, "Extra inputs are not permitted"),
    ],
)
async def test_invalid_queries_are_422(
    api: AsyncClient, params: dict[str, Any], message: str
) -> None:
    response = await api.get("/v1/earthquakes", params=params)

    assert response.status_code == 422
    assert message in response.text


async def test_exactly_366_days_is_allowed_one_microsecond_more_is_not(api: AsyncClient) -> None:
    end = datetime.now(UTC)  # one instant for both bounds
    exactly = {"start": (end - timedelta(days=366)).isoformat(), "end": end.isoformat()}
    over = {**exactly, "start": (end - timedelta(days=366, microseconds=1)).isoformat()}

    assert (await api.get("/v1/earthquakes", params=exactly)).status_code == 200
    assert (await api.get("/v1/earthquakes", params=over)).status_code == 422


# --- pagination ---------------------------------------------------------------------------


async def walk(api: AsyncClient, params: dict[str, Any]) -> list[list[str]]:
    pages, cursor = [], None
    while True:
        body = (
            await api.get(
                "/v1/earthquakes", params={**params, **({"cursor": cursor} if cursor else {})}
            )
        ).json()
        pages.append([quake["id"] for quake in body["data"]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


async def test_pagination_is_stable_with_rows_sharing_occurred_at(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    tied = [await seed_quake(db_session, occurred_at=DEFAULT_TIME) for _ in range(7)]
    older = [
        await seed_quake(db_session, occurred_at=DEFAULT_TIME - timedelta(minutes=m))
        for m in (1, 2, 3)
    ]
    expected = [str(r.id) for r in sorted(tied, key=lambda r: r.id, reverse=True)] + [
        str(r.id) for r in older
    ]

    pages = await walk(api, {"limit": 3})

    assert [len(page) for page in pages] == [3, 3, 3, 1]
    assert [quake_id for page in pages for quake_id in page] == expected


async def test_rows_inserted_while_paging_do_not_shift_later_pages(
    api: AsyncClient, db_session: AsyncSession
) -> None:
    rows = [
        await seed_quake(db_session, occurred_at=DEFAULT_TIME - timedelta(minutes=m))
        for m in range(6)
    ]
    first = (await api.get("/v1/earthquakes", params={"limit": 2})).json()

    # A new quake arrives, newer than everything already paged past.
    await seed_quake(db_session, occurred_at=DEFAULT_TIME + timedelta(minutes=5))
    rest = await walk(api, {"limit": 2, "cursor": first["next_cursor"]})

    seen = [q["id"] for q in first["data"]] + [i for page in rest for i in page]
    assert seen == [str(r.id) for r in rows]  # every original row exactly once, in order


async def test_last_page_has_no_next_cursor(api: AsyncClient, db_session: AsyncSession) -> None:
    for m in range(2):
        await seed_quake(db_session, occurred_at=DEFAULT_TIME - timedelta(minutes=m))

    body = (await api.get("/v1/earthquakes", params={"limit": 2})).json()

    assert len(body["data"]) == 2
    assert body["next_cursor"] is None
