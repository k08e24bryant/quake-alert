from typing import Literal

from pydantic import BaseModel, Field


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    db: Literal["ok", "error"] = Field(
        description="PostgreSQL. `error` makes the API not ready (503): it cannot serve data."
    )
    redis: Literal["ok", "degraded"] = Field(
        description=(
            "Redis. `degraded` still means ready (200): the API serves from the database "
            "without cache or rate limiting."
        )
    )
