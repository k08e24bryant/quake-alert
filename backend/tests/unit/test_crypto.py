import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.crypto import (
    SecretBox,
    SecretBoxError,
    check_startup_secrets,
    secret_box_from_settings,
    webhook_keys,
)
from tests.webhook_samples import FERNET_KEY, OTHER_FERNET_KEY


def settings(environment: str, keys: str) -> Settings:
    return Settings(environment=environment, webhook_secret_keys=SecretStr(keys))  # type: ignore[arg-type]


def test_round_trip_and_ciphertext_is_not_the_secret() -> None:
    box = SecretBox([FERNET_KEY])

    encrypted = box.encrypt("whsec_abc")

    assert "whsec_abc" not in encrypted
    assert box.decrypt(encrypted) == "whsec_abc"


def test_rotation_encrypts_with_the_first_key_and_decrypts_with_any() -> None:
    old = SecretBox([OTHER_FERNET_KEY])
    stored_with_old_key = old.encrypt("whsec_abc")
    rotated = SecretBox([FERNET_KEY, OTHER_FERNET_KEY])  # new key first

    assert rotated.decrypt(stored_with_old_key) == "whsec_abc"
    re_encrypted = rotated.rotate(stored_with_old_key)
    assert SecretBox([FERNET_KEY]).decrypt(re_encrypted) == "whsec_abc"  # old key not needed


def test_is_current_only_for_the_first_key() -> None:
    rotated = SecretBox([FERNET_KEY, OTHER_FERNET_KEY])

    assert rotated.is_current(SecretBox([FERNET_KEY]).encrypt("whsec_abc"))
    assert not rotated.is_current(SecretBox([OTHER_FERNET_KEY]).encrypt("whsec_abc"))
    assert not rotated.is_current("not-a-fernet-token")


def test_unknown_key_cannot_decrypt() -> None:
    encrypted = SecretBox([OTHER_FERNET_KEY]).encrypt("whsec_abc")

    with pytest.raises(SecretBoxError):
        SecretBox([FERNET_KEY]).decrypt(encrypted)


def test_invalid_key_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ValueError, match="invalid Fernet key") as caught:
        SecretBox(["not-a-key"])

    assert "not-a-key" not in str(caught.value)


def test_keys_are_comma_separated_and_trimmed() -> None:
    assert webhook_keys(settings("development", f" {FERNET_KEY} , ,{OTHER_FERNET_KEY}")) == [
        FERNET_KEY,
        OTHER_FERNET_KEY,
    ]
    assert secret_box_from_settings(settings("development", "")) is None


def test_production_refuses_to_start_without_a_key() -> None:
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET_KEYS"):
        check_startup_secrets(settings("production", ""))


def test_production_starts_with_a_key_and_other_environments_without() -> None:
    check_startup_secrets(settings("production", FERNET_KEY))
    check_startup_secrets(settings("development", ""))
    check_startup_secrets(settings("test", ""))


def test_an_invalid_key_never_starts() -> None:
    with pytest.raises(ValueError, match="invalid Fernet key"):
        check_startup_secrets(settings("development", "garbage"))
