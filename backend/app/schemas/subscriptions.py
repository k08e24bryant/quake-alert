import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class WebhookSubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        max_length=2048,
        description=(
            "Where alerts are POSTed. https only (http is accepted in development only). "
            "Must resolve to public addresses; checked again before every delivery."
        ),
    )
    lat: float = Field(ge=-90, le=90, description="Rounded to 2 decimals (~1 km) when stored.")
    lon: float = Field(ge=-180, le=180, description="Rounded to 2 decimals (~1 km) when stored.")
    radius_km: int = Field(ge=10, le=1000)
    min_magnitude: Decimal = Field(ge=Decimal("2.0"), le=Decimal("9.0"), decimal_places=1)


class WebhookSubscriptionCreated(BaseModel):
    id: uuid.UUID
    url: str
    latitude: float
    longitude: float
    radius_km: int
    min_magnitude: float
    is_active: bool = True
    signing_secret: str = Field(
        description=(
            "HMAC-SHA256 key for X-Quake-Signature. Shown once: store it now. See docs/webhooks.md."
        )
    )
    manage_token: str = Field(
        description=(
            "Send as `Authorization: Bearer <token>` to delete or test this subscription. "
            "Shown once: store it now."
        )
    )


class WebhookTestResult(BaseModel):
    delivered: bool = Field(description="The receiver answered 2xx.")
    status_code: int | None = Field(description="The receiver's status code, if it answered.")
    error: str | None = Field(description="Why it was not delivered.")
