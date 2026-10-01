"""The radius query must be able to use the GIST index on earthquakes.location.

Planner choices depend on data volume, so this seeds a realistic amount of synthetic data
(20k quakes spread over Indonesia, a year of history) and runs ANALYZE first. Everything is
inside the rolled-back test transaction.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.earthquakes.service import build_list_query
from app.schemas.earthquakes import EarthquakeQuery
from tests.integration.seed import JAKARTA

SEED_20K = text(
    """
    INSERT INTO earthquakes
        (occurred_at, magnitude, depth_km, location, region, source_feeds, fingerprint, raw)
    SELECT
        now() - random() * interval '365 days',
        round((2 + random() * 5)::numeric, 1),
        (random() * 300)::int,
        ST_SetSRID(ST_MakePoint(95 + random() * 46, -11 + random() * 17), 4326)::geography,
        'synthetic',
        ARRAY['gempaterkini'],
        md5('synthetic-' || i),
        '{}'::jsonb
    FROM generate_series(1, 20000) AS i
    """
)


async def explain(session: AsyncSession, params: EarthquakeQuery) -> str:
    sql = build_list_query(params).compile(
        dialect=session.get_bind().dialect, compile_kwargs={"literal_binds": True}
    )
    rows: list[str] = list((await session.execute(text(f"EXPLAIN {sql}"))).scalars().all())
    return "\n".join(rows)


async def test_radius_query_uses_the_gist_index(db_session: AsyncSession) -> None:
    await db_session.execute(SEED_20K)
    await db_session.execute(text("ANALYZE earthquakes"))
    lat, lon = JAKARTA

    plan = await explain(db_session, EarthquakeQuery(lat=lat, lon=lon, radius_km=100))

    assert "ix_earthquakes_location" in plan, plan
    assert "Seq Scan" not in plan, plan
