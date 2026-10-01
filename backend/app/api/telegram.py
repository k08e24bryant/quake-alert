from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import SessionDep
from app.core.security import verify_telegram_secret
from app.notifications.bot import handle_update

# Not rate limited: every request comes from Telegram's servers, and a 429 would only make
# Telegram retry. The secret token is what keeps everyone else out.
router = APIRouter(
    prefix="/v1/telegram",
    tags=["telegram"],
    dependencies=[Depends(verify_telegram_secret)],
    responses={status.HTTP_403_FORBIDDEN: {"description": "Missing or wrong secret token"}},
)


@router.post("/webhook")
async def telegram_webhook(
    payload: Annotated[dict[str, Any], Body()], session: SessionDep
) -> Response:
    """Receives Telegram updates (set with setWebhook and a secret_token). The bot's reply
    goes back in the response body as a sendMessage call, which Telegram executes."""
    reply = await handle_update(session, payload)
    if reply is None:
        return Response(status_code=status.HTTP_200_OK)
    return JSONResponse({"method": "sendMessage", **reply.send_message_params()})
