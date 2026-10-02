import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import formatdate
from pathlib import Path
from typing import Any

import pytest

from app.notifications.base import AlertData
from app.notifications.messages import DISCLAIMER
from app.notifications.webhook import (
    alert_payload,
    encode,
    retry_after_seconds,
    sign,
    verification_payload,
    webhook_test_payload,
)
from tests.webhook_samples import expected_signature

DOCS = Path(__file__).resolve().parents[3] / "docs" / "webhooks.md"


def alert(**overrides: Any) -> AlertData:
    fields: dict[str, Any] = {
        "delivery_id": uuid.UUID("6c1d9e2a-8f3b-4a77-b1c0-5e2f7d9a3b48"),
        "earthquake_id": uuid.UUID("724a3801-0aac-4382-9b02-37d518091d01"),
        "occurred_at": datetime(2026, 10, 1, 6, 24, 52, tzinfo=UTC),
        "magnitude": Decimal("5.2"),
        "depth_km": 25,
        "latitude": -2.46,
        "longitude": 140.38,
        "region": "Pusat gempa berada di darat 15 km Barat Laut Sentani",
        "potential": "Gempa ini dirasakan untuk diteruskan pada masyarakat",
        "felt": "II Kab. Jayapura",
        "shakemap_url": None,
        "source_feeds": ["autogempa", "gempadirasakan"],
        "distance_km": 118.4,
        "possible_duplicate_of": None,
        "is_synthetic": False,
    }
    return AlertData(**{**fields, **overrides})


def test_alert_payload_shape() -> None:
    body = alert_payload(alert())

    assert body == {
        "schema_version": 1,
        "event": "earthquake.alert",
        "delivery_id": "6c1d9e2a-8f3b-4a77-b1c0-5e2f7d9a3b48",
        "test": False,
        "synthetic": False,
        "possible_duplicate": False,
        "earthquake": {
            "id": "724a3801-0aac-4382-9b02-37d518091d01",
            "occurred_at": "2026-10-01T06:24:52+00:00",
            "magnitude": 5.2,
            "depth_km": 25,
            "latitude": -2.46,
            "longitude": 140.38,
            "region": "Pusat gempa berada di darat 15 km Barat Laut Sentani",
            "potential": "Gempa ini dirasakan untuk diteruskan pada masyarakat",
            "potential_label": "Potensi (BMKG)",
            "felt": "II Kab. Jayapura",
            "shakemap_url": None,
            "source_feeds": ["autogempa", "gempadirasakan"],
            "distance_km": 118.4,
        },
        "source": {"notice": "Sumber: BMKG", "url": "https://www.bmkg.go.id"},
        "disclaimer": DISCLAIMER,
    }
    assert "tsunami" not in json.dumps(body).lower()  # nothing labels Potensi as tsunami info


def test_possible_duplicate_flag() -> None:
    previous = datetime(2026, 10, 1, 6, 24, 10, tzinfo=UTC)

    assert alert_payload(alert(possible_duplicate_of=previous))["possible_duplicate"] is True


def test_synthetic_quake_payload_is_labelled() -> None:
    body = alert_payload(alert(is_synthetic=True))

    assert (body["event"], body["synthetic"], body["test"]) == ("earthquake.test", True, False)


def test_webhook_test_payload_carries_no_quake() -> None:
    body = webhook_test_payload(uuid.uuid4())

    assert (body["event"], body["test"], body["synthetic"]) == ("webhook.test", True, False)
    assert body["earthquake"] is None
    assert body["disclaimer"] == DISCLAIMER


def test_verification_payload_carries_the_challenge_and_no_quake() -> None:
    body = verification_payload(uuid.UUID(int=1), "the-challenge")

    assert (body["event"], body["test"], body["synthetic"]) == (
        "webhook.verification",
        True,
        False,
    )
    assert (body["challenge"], body["earthquake"]) == ("the-challenge", None)
    assert body["source"] == {"notice": "Sumber: BMKG", "url": "https://www.bmkg.go.id"}
    assert body["disclaimer"] == DISCLAIMER


NOW = 1_790_851_492.0


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("120", 120.0),
        (" 120 ", 120.0),
        ("0", 0.0),
        (formatdate(NOW + 90, usegmt=True), 90.0),  # IMF-fixdate
        ("Thu, 01 Oct 2026 10:46:22 GMT", 90.0),  # the same, spelled out
        ("Thursday, 01-Oct-26 10:46:22 GMT", 90.0),  # obsolete RFC 850 form
        ("Thu Oct  1 10:46:22 2026", 90.0),  # obsolete asctime form
        (formatdate(NOW - 600, usegmt=True), 0.0),  # already past
        (None, None),
        ("", None),
        ("soon", None),
        ("-5", None),
        ("1.5", None),
        ("\uff11\uff12", None),  # full-width digits are not delay-seconds
    ],
)
def test_retry_after_parsing(header: str | None, seconds: float | None) -> None:
    assert retry_after_seconds(header, NOW) == seconds


def test_signature_is_hmac_sha256_of_timestamp_dot_body() -> None:
    body = encode(alert_payload(alert()))

    signature = sign("whsec_test", 1_790_851_492, body)

    assert signature == expected_signature("whsec_test", "1790851492", body)
    assert re.fullmatch(r"sha256=[0-9a-f]{64}", signature)


def test_encoding_is_compact_utf8() -> None:
    body = encode({"region": "Barat Daya Kab. Pidie Jaya", "x": 1})

    assert body == b'{"region":"Barat Daya Kab. Pidie Jaya","x":1}'


# --- the receiver example in docs/webhooks.md is real, working code ---------------------------


@pytest.fixture(scope="module")
def docs_example() -> dict[str, Any]:
    blocks = re.findall(r"```python\n(.*?)```", DOCS.read_text(encoding="utf-8"), re.DOTALL)
    [code] = [block for block in blocks if "def verify_quake_webhook" in block]
    namespace: dict[str, Any] = {"__name__": "docs_example"}  # skips the __main__ server
    exec(compile(code, str(DOCS), "exec"), namespace)  # noqa: S102  (our own docs)
    return namespace


@pytest.fixture(scope="module")
def verify_quake_webhook(docs_example: dict[str, Any]) -> Callable[..., bool]:
    function: Callable[..., bool] = docs_example["verify_quake_webhook"]
    return function


@pytest.fixture(scope="module")
def handle_quake_webhook(docs_example: dict[str, Any]) -> Callable[..., tuple[int, bytes]]:
    function: Callable[..., tuple[int, bytes]] = docs_example["handle_quake_webhook"]
    return function


def signed_request(secret: str, timestamp: int) -> tuple[dict[str, str], bytes]:
    body = encode(alert_payload(alert()))
    headers = {
        "x-quake-timestamp": str(timestamp),
        "x-quake-signature": sign(secret, timestamp, body),
        "x-quake-delivery-id": "6c1d9e2a-8f3b-4a77-b1c0-5e2f7d9a3b48",
    }
    return headers, body


def test_docs_receiver_accepts_a_fresh_correctly_signed_request(
    verify_quake_webhook: Callable[..., bool],
) -> None:
    headers, body = signed_request("whsec_test", 1_000_000)

    assert verify_quake_webhook("whsec_test", headers, body, now=1_000_000 + 60)


@pytest.mark.parametrize(
    ("change", "now_offset"),
    [
        ("wrong_secret", 0),
        ("tampered_body", 0),
        ("tampered_timestamp", 0),
        ("none", 301),  # older than 5 minutes: a replay
        ("none", -301),  # too far in the future
        ("garbage_timestamp", 0),
        ("missing_signature", 0),
    ],
)
def test_docs_receiver_rejects(
    verify_quake_webhook: Callable[..., bool], change: str, now_offset: int
) -> None:
    headers, body = signed_request("whsec_test", 1_000_000)
    key = "whsec_test"
    if change == "wrong_secret":
        key = "whsec_other"
    elif change == "tampered_body":
        body = body.replace(b"5.2", b"7.2")
    elif change == "tampered_timestamp":
        headers["x-quake-timestamp"] = "1000001"
    elif change == "garbage_timestamp":
        headers["x-quake-timestamp"] = "1e6"
    elif change == "missing_signature":
        del headers["x-quake-signature"]

    assert not verify_quake_webhook(key, headers, body, now=1_000_000 + now_offset)


def signed(secret: str, payload: dict[str, Any], timestamp: int) -> tuple[dict[str, str], bytes]:
    body = encode(payload)
    headers = {
        "x-quake-timestamp": str(timestamp),
        "x-quake-signature": sign(secret, timestamp, body),
        "x-quake-delivery-id": payload["delivery_id"],
    }
    return headers, body


def test_docs_receiver_echoes_a_correctly_signed_challenge(
    handle_quake_webhook: Callable[..., tuple[int, bytes]],
) -> None:
    headers, body = signed(
        "whsec_test", verification_payload(uuid.uuid4(), "Zq3x-challenge"), 1_000_000
    )

    status, answer = handle_quake_webhook("whsec_test", headers, body, set(), now=1_000_000)

    assert status == 200
    assert json.loads(answer) == {"challenge": "Zq3x-challenge"}


def test_docs_receiver_refuses_a_challenge_it_cannot_verify(
    handle_quake_webhook: Callable[..., tuple[int, bytes]],
) -> None:
    # E.g. the creation-time attempt, before the receiver was given the signing secret.
    headers, body = signed(
        "whsec_new", verification_payload(uuid.uuid4(), "Zq3x-challenge"), 1_000_000
    )

    status, answer = handle_quake_webhook("whsec_old", headers, body, set(), now=1_000_000)

    assert (status, answer) == (401, b"")


def test_docs_receiver_acknowledges_alerts_once(
    handle_quake_webhook: Callable[..., tuple[int, bytes]],
) -> None:
    seen: set[str] = set()
    alert_request = signed("whsec_test", alert_payload(alert()), 1_000_000)
    test_request = signed("whsec_test", webhook_test_payload(uuid.uuid4()), 1_000_000)

    first = handle_quake_webhook("whsec_test", *alert_request, seen, now=1_000_000)
    retry = handle_quake_webhook("whsec_test", *alert_request, seen, now=1_000_000)
    test = handle_quake_webhook("whsec_test", *test_request, seen, now=1_000_000)

    assert first == retry == test == (204, b"")
    assert seen == {"6c1d9e2a-8f3b-4a77-b1c0-5e2f7d9a3b48", test_request[0]["x-quake-delivery-id"]}
