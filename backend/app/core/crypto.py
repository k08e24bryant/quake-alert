"""Encryption for secrets we must be able to read back (webhook signing secrets).

MultiFernet over WEBHOOK_SECRET_KEYS: encrypt with the first key, decrypt with any of them,
so a key can be rotated by putting the new one first and keeping the old ones until every
stored secret has been re-encrypted (see README).
"""

from collections.abc import Sequence

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import Settings


class SecretBoxError(Exception):
    """A stored secret could not be decrypted with any configured key."""


class SecretBox:
    def __init__(self, keys: Sequence[str]) -> None:
        if not keys:
            raise ValueError("at least one key is required")
        try:
            self._fernet = MultiFernet([Fernet(key) for key in keys])
        except ValueError as exc:  # never echo the key itself
            raise ValueError("WEBHOOK_SECRET_KEYS contains an invalid Fernet key") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            raise SecretBoxError("no configured key decrypts this secret") from None

    def rotate(self, token: str) -> str:
        """Re-encrypt with the first (newest) key."""
        try:
            return self._fernet.rotate(token.encode()).decode()
        except InvalidToken:
            raise SecretBoxError("no configured key decrypts this secret") from None


def webhook_keys(settings: Settings) -> list[str]:
    raw = settings.webhook_secret_keys.get_secret_value()
    return [key.strip() for key in raw.split(",") if key.strip()]


def secret_box_from_settings(settings: Settings) -> SecretBox | None:
    """None when no key is configured (allowed outside production)."""
    keys = webhook_keys(settings)
    return SecretBox(keys) if keys else None


def check_startup_secrets(settings: Settings) -> None:
    """Called by the API and the worker at startup: invalid keys never start, and in
    production missing keys don't either."""
    box = secret_box_from_settings(settings)
    if box is None and settings.environment == "production":
        raise RuntimeError("WEBHOOK_SECRET_KEYS must be set in production")
