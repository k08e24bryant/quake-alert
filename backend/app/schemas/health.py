from typing import Literal

from pydantic import BaseModel

CheckStatus = Literal["ok", "error"]


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: CheckStatus
    checks: dict[str, CheckStatus]
