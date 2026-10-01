from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.redis import create_redis
from app.db.session import create_engine, create_sessionmaker


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(app_settings.log_level)
        engine = create_engine(app_settings)
        redis = create_redis(app_settings.redis_url)
        app.state.settings = app_settings
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.redis = redis
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="Quake Alert", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    return app


app = create_app()
