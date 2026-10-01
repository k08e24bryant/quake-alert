"""arq job definitions. `ctx` is arq's per-worker dict; startup() fills it."""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.db.session import create_engine, create_sessionmaker
from app.earthquakes.cache import invalidate_latest
from app.ingestion.bmkg_client import BmkgClient, create_http_client
from app.ingestion.retention import prune_ingestion_runs
from app.ingestion.service import ingest_all_feeds

logger = logging.getLogger(__name__)


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

    async def invalidate_api_cache() -> None:
        # ctx["redis"] is arq's own connection, to the same Redis the API caches in.
        await invalidate_latest(ctx["redis"])

    results = await ingest_all_feeds(
        ctx["session_factory"], ctx["bmkg_client"], ctx["settings"], on_change=invalidate_api_cache
    )
    return {result.feed.value: result.status.value for result in results}


async def prune_old_ingestion_runs(ctx: dict[str, Any]) -> int:
    """Daily retention for ingestion_runs; see app.ingestion.retention."""
    async with ctx["session_factory"]() as session, session.begin():
        deleted = await prune_ingestion_runs(session, datetime.now(UTC), ctx["settings"])
    logger.info("pruned ingestion_runs", extra={"deleted": deleted})
    return deleted
