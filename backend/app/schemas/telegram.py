"""The parts of Telegram's Update object the bot reads. Everything else is ignored."""

from pydantic import BaseModel, ConfigDict, Field


class _TelegramModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Chat(_TelegramModel):
    id: int
    type: str  # "private", "group", "supergroup" or "channel"


class Location(_TelegramModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class Message(_TelegramModel):
    message_id: int
    chat: Chat
    text: str | None = None
    location: Location | None = None


class Update(_TelegramModel):
    update_id: int
    message: Message | None = None
