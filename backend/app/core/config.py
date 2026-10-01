from functools import lru_cache
from typing import Literal

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
