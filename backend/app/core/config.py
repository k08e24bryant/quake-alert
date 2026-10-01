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

    readiness_timeout_seconds: float = 2.0

    bmkg_base_url: str = "https://data.bmkg.go.id/DataMKG/TEWS/"
    bmkg_timeout_seconds: float = Field(default=10.0, gt=0)
    bmkg_max_attempts: int = Field(default=3, ge=1)
    # Delay before retry n is backoff * 2**(n-1).
    bmkg_retry_backoff_seconds: float = Field(default=1.0, ge=0)

    # Two reports are the same quake if they are this close in time AND space.
    dedup_max_time_diff_seconds: int = Field(default=60, ge=0)
    dedup_max_distance_km: float = Field(default=50.0, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
