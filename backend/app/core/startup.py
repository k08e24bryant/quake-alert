"""Startup checks shared by the API and the worker: in production, refuse to start rather
than run with a missing or default setting.

A required setting must be set explicitly (environment or .env), not empty, and not still
the CHANGE_ME placeholder from deploy/.env.production.example. Its built-in default
doesn't count: DATABASE_URL's default points at localhost, which in a
container means "no database", and an empty TELEGRAM_WEBHOOK_SECRET would make the bot
reject every update. Errors name the variables, never their values.
"""

from pydantic import SecretStr

from app.core.config import Settings
from app.core.crypto import check_startup_secrets

PLACEHOLDER = "CHANGE_ME"

REQUIRED_IN_PRODUCTION = (
    "database_url",
    "redis_url",
    "webhook_secret_keys",
    "telegram_bot_token",
    "telegram_webhook_secret",
    "cors_allowed_origins",
    # Behind Caddy it must be true, or every client shares the proxy's rate limit. Without
    # a proxy it must be false, or clients can forge their address. Either way: a decision.
    "trust_proxy_headers",
)


def missing_production_settings(settings: Settings) -> list[str]:
    """Environment variable names of required settings that are unset, empty or still a
    placeholder; always empty outside production."""
    if settings.environment != "production":
        return []
    missing = []
    for name in REQUIRED_IN_PRODUCTION:
        value = getattr(settings, name)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        unset = name not in settings.model_fields_set
        if unset or (isinstance(value, str) and (not value.strip() or PLACEHOLDER in value)):
            missing.append(name.upper())
    return missing


def check_startup_settings(settings: Settings) -> None:
    """Called by the API (lifespan) and the worker (on_startup) before anything else."""
    missing = missing_production_settings(settings)
    if missing:
        raise RuntimeError(
            "refusing to start in production: required settings are missing or empty: "
            + ", ".join(missing)
            + " (see deploy/.env.production.example)"
        )
    check_startup_secrets(settings)
