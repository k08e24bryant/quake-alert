import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_TELEGRAM_SECRET = re.compile(r"[A-Za-z0-9_-]{1,256}")


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
    # Circuit breaker around the API's Redis calls: after this many consecutive failures or
    # timeouts, skip Redis entirely for REDIS_BREAKER_OPEN_SECONDS, then try one call.
    redis_breaker_failure_threshold: int = Field(default=3, ge=1)
    redis_breaker_open_seconds: float = Field(default=30.0, gt=0)

    # /readyz database check. (Its Redis check uses redis_socket_timeout_seconds.)
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
    # Ingestion is "stale" (GET /v1/status) when some feed has had no successful run
    # (status success or skipped) for this long.
    ingestion_stale_after_minutes: int = Field(default=5, ge=1)

    # Query API caching. `latest` is also invalidated by ingestion; the TTL only bounds
    # staleness if an invalidation is missed (e.g. Redis briefly unreachable from the worker).
    cache_latest_ttl_seconds: int = Field(default=60, ge=1)
    cache_list_ttl_seconds: int = Field(default=30, ge=1)

    # Per-client-IP fixed-window rate limit for /v1.
    rate_limit_per_minute: int = Field(default=60, ge=1)
    # Only enable behind a proxy you control (Caddy): clients can forge X-Forwarded-For.
    trust_proxy_headers: bool = False

    # Browser origins (comma-separated, exact, e.g. https://quake.example.com) allowed to
    # GET /v1/earthquakes* and /v1/status. Empty: no CORS. "*" is refused in production.
    cors_allowed_origins: str = ""

    # Telegram Bot API, called directly with httpx. Empty = not configured: the webhook
    # rejects every request and deliveries fail without calling Telegram.
    telegram_bot_token: SecretStr = SecretStr("")
    # Compared with the X-Telegram-Bot-Api-Secret-Token header on POST /v1/telegram/webhook;
    # pass the same value as secret_token to setWebhook.
    telegram_webhook_secret: SecretStr = SecretStr("")
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_timeout_seconds: float = Field(default=10.0, gt=0)

    # Only quakes whose origin time is at most this old are notified, both when matching
    # and when sending (a retry that ends up later than this gives up instead).
    notify_max_age_minutes: int = Field(default=30, ge=1)
    # Tries per delivery, including the first. Delay before retry n is backoff * 2**(n-1),
    # except after a Telegram 429, which waits for its retry_after.
    notify_max_attempts: int = Field(default=5, ge=1)
    notify_retry_backoff_seconds: float = Field(default=5.0, ge=0)
    # A subscriber already alerted for another row within BOTH of these is told the new
    # alert may be the same event reported by another BMKG feed (dedup keeps such duplicates).
    notify_duplicate_window_seconds: int = Field(default=120, ge=0)
    notify_duplicate_distance_km: float = Field(default=100.0, ge=0)
    # Webhook channel. Signing secrets are stored encrypted with these Fernet keys
    # (comma-separated): encrypt with the first, decrypt with any, so keys can be rotated.
    # Required in production: the API and the worker refuse to start without one.
    webhook_secret_keys: SecretStr = SecretStr("")
    # Connect, read, write and pool timeouts of one webhook POST.
    webhook_timeout_seconds: float = Field(default=5.0, gt=0)
    # At most this much of a receiver's response body is read (it is only kept for errors).
    webhook_max_response_bytes: int = Field(default=65536, ge=0)
    # A webhook subscription is deactivated after this many failed deliveries in a row.
    webhook_max_consecutive_failures: int = Field(default=10, ge=1)
    # A receiver's 429 is retried after its Retry-After, but never waits longer than this.
    webhook_max_retry_after_seconds: int = Field(default=300, ge=0)
    # Daily prune: webhook subscriptions still pending verification this long after they
    # were created are deleted.
    webhook_pending_verification_max_age_hours: int = Field(default=24, ge=1)
    # Per client IP, on top of RATE_LIMIT_PER_MINUTE: POST /v1/subscriptions/webhook and
    # its .../test and .../verify share this hourly budget.
    subscription_write_rate_limit_per_hour: int = Field(default=5, ge=1)

    # Daily prune: sent/failed deliveries older than this are deleted; pending never are.
    notification_deliveries_retention_days: int = Field(default=30, ge=1)

    @field_validator("telegram_webhook_secret")
    @classmethod
    def _telegram_secret_format(cls, value: SecretStr) -> SecretStr:
        # What setWebhook accepts as secret_token; anything else would be refused there.
        secret = value.get_secret_value()
        if secret and not _TELEGRAM_SECRET.fullmatch(secret):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must be 1-256 characters of A-Z a-z 0-9 _ -")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
