from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.core.startup import (
    REQUIRED_IN_PRODUCTION,
    check_startup_settings,
    missing_production_settings,
)
from tests.webhook_samples import FERNET_KEY

PRODUCTION: dict[str, Any] = {
    "environment": "production",
    "database_url": "postgresql+asyncpg://quake:db-password-123@db:5432/quake_alert",
    "redis_url": "redis://:redis-password-456@redis:6379/0",
    "webhook_secret_keys": SecretStr(FERNET_KEY),
    "telegram_bot_token": SecretStr("123456:prod-token-789"),
    "telegram_webhook_secret": SecretStr("webhook-secret-abc"),
    "cors_allowed_origins": "https://gempasekitarsaya.my.id,https://www.gempasekitarsaya.my.id",
    "trust_proxy_headers": True,
}
_UNSET = object()  # leave this setting out entirely
SECRET_VALUES = ("db-password-123", "redis-password-456", "prod-token-789", "webhook-secret-abc")


def settings(**overrides: Any) -> Settings:
    values = {**PRODUCTION, **overrides}
    for key in [k for k, v in values.items() if v is _UNSET]:
        del values[key]
    return Settings(_env_file=None, **values)


def test_a_complete_production_config_starts() -> None:
    assert missing_production_settings(settings()) == []
    check_startup_settings(settings())


@pytest.mark.parametrize("name", REQUIRED_IN_PRODUCTION)
def test_each_required_setting_left_unset_refuses_to_start(name: str) -> None:
    incomplete = settings(**{name: _UNSET})

    assert missing_production_settings(incomplete) == [name.upper()]
    with pytest.raises(RuntimeError, match=name.upper()) as caught:
        check_startup_settings(incomplete)
    for secret in SECRET_VALUES:
        assert secret not in str(caught.value)


@pytest.mark.parametrize(
    "name",
    [
        "database_url",
        "redis_url",
        "telegram_bot_token",
        "telegram_webhook_secret",
        "cors_allowed_origins",
    ],
)
def test_an_empty_value_counts_as_missing(name: str) -> None:
    value: Any = SecretStr("  ") if isinstance(PRODUCTION[name], SecretStr) else "  "
    if name == "telegram_webhook_secret":
        value = SecretStr("")  # spaces would already fail the format check
    assert missing_production_settings(settings(**{name: value})) == [name.upper()]


def test_trust_proxy_headers_false_is_a_valid_explicit_choice() -> None:
    assert missing_production_settings(settings(trust_proxy_headers=False)) == []


def test_all_missing_are_listed_at_once() -> None:
    bare = Settings(_env_file=None, environment="production")

    assert missing_production_settings(bare) == [n.upper() for n in REQUIRED_IN_PRODUCTION]


def test_values_from_the_environment_count_as_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in PRODUCTION.items():
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        monkeypatch.setenv(name.upper(), raw)

    from_env = Settings(_env_file=None)

    assert from_env.environment == "production"
    assert missing_production_settings(from_env) == []


@pytest.mark.parametrize("environment", ["development", "test"])
def test_nothing_is_required_outside_production(environment: str) -> None:
    bare = Settings(_env_file=None, environment=environment)  # type: ignore[arg-type]

    assert missing_production_settings(bare) == []
    check_startup_settings(bare)


@pytest.mark.parametrize("secret", ["has space", "semi;colon", "x" * 257, "émoji"])
def test_telegram_webhook_secret_must_be_valid_for_set_webhook(secret: str) -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_SECRET"):
        settings(telegram_webhook_secret=SecretStr(secret))


def test_telegram_webhook_secret_accepts_the_allowed_alphabet() -> None:
    settings(telegram_webhook_secret=SecretStr("Az09_-" * 10))


async def test_the_worker_refuses_to_start_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from worker import jobs

    monkeypatch.setattr(jobs, "get_settings", lambda: settings(telegram_bot_token=_UNSET))

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        await jobs.startup({})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("telegram_bot_token", SecretStr("CHANGE_ME")),
        ("database_url", "postgresql+asyncpg://quake:CHANGE_ME@db:5432/quake_alert"),
        ("cors_allowed_origins", "CHANGE_ME"),
    ],
)
def test_the_example_placeholder_counts_as_missing(name: str, value: Any) -> None:
    assert missing_production_settings(settings(**{name: value})) == [name.upper()]
