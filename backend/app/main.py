import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import earthquakes, health, status, subscriptions, telegram
from app.core.circuit_breaker import CircuitBreaker
from app.core.config import Settings, get_settings
from app.core.cors import ReadApiCORSMiddleware, cors_origins
from app.core.crypto import check_startup_secrets
from app.core.logging import configure_logging
from app.core.rate_limit import RateLimiter
from app.core.redis import GuardedRedis, create_redis
from app.db.session import create_engine, create_sessionmaker
from app.notifications.webhook import WebhookNotifier, create_webhook_http_client


def create_app(
    settings: Settings | None = None,
    *,
    breaker_clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(app_settings.log_level)
        check_startup_secrets(app_settings)  # e.g. no WEBHOOK_SECRET_KEYS in production
        engine = create_engine(app_settings)
        redis = create_redis(app_settings.redis_url, app_settings.redis_socket_timeout_seconds)
        app.state.settings = app_settings
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        guarded_redis = GuardedRedis(
            redis,
            CircuitBreaker(
                name="redis",
                failure_threshold=app_settings.redis_breaker_failure_threshold,
                open_seconds=app_settings.redis_breaker_open_seconds,
                call_timeout_seconds=app_settings.redis_socket_timeout_seconds,
                clock=breaker_clock,
            ),
        )
        app.state.redis = redis  # raw: only /readyz, which must really probe Redis
        app.state.guarded_redis = guarded_redis
        app.state.rate_limiter = RateLimiter(
            guarded_redis, limit=app_settings.rate_limit_per_minute
        )
        app.state.subscription_write_limiter = RateLimiter(
            guarded_redis,
            limit=app_settings.subscription_write_rate_limit_per_hour,
            window_seconds=3600,
            name="subscription-write",
        )
        webhook_http = create_webhook_http_client()
        app.state.webhook_notifier = WebhookNotifier.from_settings(webhook_http, app_settings)
        try:
            yield
        finally:
            await webhook_http.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="Quake Alert", version="0.1.0", lifespan=lifespan)
    # Validated here, so a bad CORS_ALLOWED_ORIGINS (or "*" in production) never starts.
    origins = cors_origins(app_settings)
    if origins:
        app.add_middleware(ReadApiCORSMiddleware, allow_origins=origins)
    app.include_router(health.router)
    app.include_router(earthquakes.router)
    app.include_router(status.router)
    app.include_router(telegram.router)
    app.include_router(subscriptions.router)
    return app


app = create_app()
