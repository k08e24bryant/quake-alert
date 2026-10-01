from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: str = "postgresql+asyncpg://quake:quake@localhost:5432/quake_alert"
    database_pool_size: int = 5
    database_max_overflow: int = 5

    redis_url: str = "redis://localhost:6379/0"
    # Short, so a dead Redis degrades the API (cache bypass, no rate limiting) instead of
    # stalling every request.
    redis_socket_timeout_seconds: float = Field(default=0.5, gt=0)

    readiness_timeout_seconds: float = 2.0

    bmkg_base_url: str = "https://data.bmkg.go.id/DataMKG/TEWS/"
    bmkg_timeout_seconds: float = Field(default=10.0, gt=0)
    bmkg_max_attempts: int = Field(default=3, ge=1)
    # Delay before retry n is backoff * 2**(n-1).
    bmkg_retry_backoff_seconds: float = Field(default=1.0, ge=0)

    # Two reports are the same quake if they are this close in time AND space.
    dedup_max_time_diff_seconds: int = Field(default=60, ge=0)
    dedup_max_distance_km: float = Field(default=50.0, ge=0)
    # A same-feed report with an identical DateTime is a revision only if it is within this
    # distance of that feed's previous coordinates; farther away it is a different quake.
    same_feed_revision_max_km: float = Field(default=100.0, ge=0)

    # Retention for ingestion_runs. The latest successful run per feed is always kept,
    # because the content-hash skip compares against it.
    ingestion_runs_retention_days: int = Field(default=14, ge=1)  # success and skipped runs
    ingestion_runs_failed_retention_days: int = Field(default=90, ge=1)

    # Query API caching. `latest` is also invalidated by ingestion; the TTL only bounds
    # staleness if an invalidation is missed (e.g. Redis briefly unreachable from the worker).
    cache_latest_ttl_seconds: int = Field(default=60, ge=1)
    cache_list_ttl_seconds: int = Field(default=30, ge=1)

    # Per-client-IP fixed-window rate limit for /v1.
    rate_limit_per_minute: int = Field(default=60, ge=1)
    # Only enable behind a proxy you control (Caddy): clients can forge X-Forwarded-For.
    trust_proxy_headers: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
