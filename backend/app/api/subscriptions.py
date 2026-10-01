import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from app.api.deps import SessionDep
from app.core.rate_limit import enforce_rate_limit, enforce_subscription_write_limit
from app.notifications.base import NotifierError
from app.notifications.ssrf import DnsResolutionError, UnsafeTargetError
from app.notifications.webhook import WebhookNotifier
from app.notifications.webhook_subscriptions import (
    WebhookChannelUnavailableError,
    authenticate,
    create_webhook_subscription,
    delete_webhook_subscription,
    recipient_of,
)
from app.schemas.subscriptions import (
    WebhookSubscriptionCreate,
    WebhookSubscriptionCreated,
    WebhookTestResult,
)

router = APIRouter(
    prefix="/v1/subscriptions/webhook",
    tags=["subscriptions"],
    dependencies=[Depends(enforce_rate_limit)],
    responses={status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded"}},
)

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"description": "Unknown id, or missing/wrong token"}
}


def get_webhook_notifier(request: Request) -> WebhookNotifier:
    notifier: WebhookNotifier = request.app.state.webhook_notifier
    return notifier


def get_manage_token(authorization: Annotated[str | None, Header()] = None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None


NotifierDep = Annotated[WebhookNotifier, Depends(get_webhook_notifier)]
ManageTokenDep = Annotated[str | None, Depends(get_manage_token)]


def _not_found() -> HTTPException:
    # The same answer for an unknown id and a wrong or missing token, so ids can't be probed.
    return HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found.")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_subscription_write_limit)],
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid or unsafe URL"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Webhooks not configured"},
    },
)
async def create_subscription(
    body: WebhookSubscriptionCreate, session: SessionDep, notifier: NotifierDep
) -> WebhookSubscriptionCreated:
    """Subscribe a URL to signed alerts for quakes near a point. The signing secret and the
    manage token are in this response only: store them now."""
    try:
        created = await create_webhook_subscription(
            session,
            notifier,
            url=body.url,
            latitude=body.lat,
            longitude=body.lon,
            radius_km=body.radius_km,
            min_magnitude=body.min_magnitude,
        )
    except WebhookChannelUnavailableError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook subscriptions are not configured."
        ) from None
    except (UnsafeTargetError, DnsResolutionError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"url: {exc}") from None
    return WebhookSubscriptionCreated(
        id=created.id,
        url=created.url,
        latitude=float(created.latitude),
        longitude=float(created.longitude),
        radius_km=created.radius_km,
        min_magnitude=float(created.min_magnitude),
        signing_secret=created.signing_secret,
        manage_token=created.manage_token,
    )


@router.delete("/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND)
async def delete_subscription(
    subscription_id: uuid.UUID, token: ManageTokenDep, session: SessionDep
) -> Response:
    """Delete the subscription and its delivery history. Needs `Authorization: Bearer
    <manage_token>`."""
    if not await delete_webhook_subscription(session, subscription_id, token):
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{subscription_id}/test",
    dependencies=[Depends(enforce_subscription_write_limit)],
    responses=_NOT_FOUND,
)
async def test_subscription(
    subscription_id: uuid.UUID,
    token: ManageTokenDep,
    session: SessionDep,
    notifier: NotifierDep,
) -> WebhookTestResult:
    """Send one signed `webhook.test` payload (`"test": true`, no earthquake data) now and
    report what the receiver answered. Needs `Authorization: Bearer <manage_token>`."""
    row = await authenticate(session, subscription_id, token)
    if row is None:
        raise _not_found()
    recipient = recipient_of(row)
    await session.close()  # don't hold a connection while waiting on the receiver
    try:
        response = await notifier.send_test(recipient)
    except NotifierError as exc:
        return WebhookTestResult(delivered=False, status_code=None, error=exc.description)
    return WebhookTestResult(delivered=True, status_code=response.status_code, error=None)
