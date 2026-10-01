"""arq job definitions. `ctx` is arq's per-worker dict; startup() fills it."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.db.session import create_engine, create_sessionmaker
from app.ingestion.bmkg_client import BmkgClient, create_http_client
from app.ingestion.service import ingest_all_feeds


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    http = create_http_client(settings)
    ctx["settings"] = settings
    ctx["engine"] = engine
    ctx["session_factory"] = create_sessionmaker(engine)
    ctx["http"] = http
    ctx["bmkg_client"] = BmkgClient.from_settings(http, settings)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["http"].aclose()
    engine: AsyncEngine = ctx["engine"]
    await engine.dispose()


async def poll_bmkg_feeds(ctx: dict[str, Any]) -> dict[str, str]:
    """Fetch all BMKG feeds and ingest them; one ingestion_runs row per feed."""
    results = await ingest_all_feeds(ctx["session_factory"], ctx["bmkg_client"], ctx["settings"])
    return {result.feed.value: result.status.value for result in results}
