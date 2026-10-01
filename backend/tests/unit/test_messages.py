import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.notifications import messages
from app.notifications.messages import Alert, format_wib, render_alert
from app.notifications.subscriptions import SubscriptionView

OCCURRED_AT = datetime(2026, 10, 1, 6, 24, 52, tzinfo=UTC)  # 13:24:52 WIB

# Pictographs, dingbats, symbols, flags, variation selectors, zero-width joiner.
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿\U0001f1e6-\U0001f1ff️‍]")


def alert(**overrides: object) -> Alert:
    fields: dict[str, object] = {
        "magnitude": Decimal("5.2"),
        "region": "Pusat gempa berada di laut 52 km BaratDaya Kab. Jayapura",
        "depth_km": 25,
        "occurred_at": OCCURRED_AT,
        "distance_km": 120.4,
        "potential": "Tidak berpotensi tsunami",
        "shakemap_url": "https://data.bmkg.go.id/DataMKG/TEWS/20261001132452.mmi.jpg",
    }
    fields.update(overrides)
    return Alert(**fields)  # type: ignore[arg-type]


def test_alert_has_every_required_field() -> None:
    text = render_alert(alert())

    assert text.splitlines() == [
        "Info gempa",
        "Magnitudo: 5.2",
        "Wilayah: Pusat gempa berada di laut 52 km BaratDaya Kab. Jayapura",
        "Waktu: 01 Okt 2026 13:24:52 WIB",
        "Kedalaman: 25 km",
        "Jarak dari lokasi Anda: sekitar 120 km",
        "Potensi (BMKG): Tidak berpotensi tsunami",
        "Shakemap: https://data.bmkg.go.id/DataMKG/TEWS/20261001132452.mmi.jpg",
        "Sumber: BMKG",
    ]


def test_potential_is_verbatim_and_never_relabelled_as_tsunami_info() -> None:
    felt_notice = "Gempa ini dirasakan untuk diteruskan pada masyarakat"

    text = render_alert(alert(potential=felt_notice))

    assert f"Potensi (BMKG): {felt_notice}" in text
    assert "tsunami" not in text.lower()


def test_optional_lines_are_omitted_when_bmkg_has_no_value() -> None:
    text = render_alert(alert(potential=None, shakemap_url=None))

    assert "Potensi" not in text
    assert "Shakemap" not in text
    assert text.endswith("Sumber: BMKG")


@pytest.mark.parametrize(
    ("distance_km", "expected"),
    [(0.3, "kurang dari 1 km"), (1.0, "sekitar 1 km"), (199.6, "sekitar 200 km")],
)
def test_distance_wording(distance_km: float, expected: str) -> None:
    assert f"Jarak dari lokasi Anda: {expected}" in render_alert(alert(distance_km=distance_km))


def test_wib_crosses_midnight() -> None:
    assert format_wib(datetime(2026, 12, 31, 17, 30, tzinfo=UTC)) == "01 Jan 2027 00:30:00 WIB"


def test_possible_duplicate_is_a_prefix_line_and_the_alert_is_otherwise_unchanged() -> None:
    plain = render_alert(alert())
    previous = datetime(2026, 10, 1, 6, 24, 10, tzinfo=UTC)

    text = render_alert(alert(possible_duplicate_of=previous))

    first, rest = text.split("\n", 1)
    assert first.startswith("Catatan: mungkin kejadian yang sama")
    assert "13:24:10 WIB" in first
    assert "feed BMKG lain" in first
    assert rest == plain


def _everything_the_bot_can_say() -> list[str]:
    view = SubscriptionView(
        id=uuid.uuid4(),
        latitude=Decimal("-6.21"),
        longitude=Decimal("106.85"),
        radius_km=200,
        min_magnitude=Decimal("4.0"),
        is_active=True,
    )
    return [
        messages.START,
        messages.UNKNOWN,
        messages.NO_SUBSCRIPTION,
        messages.RADIUS_USAGE,
        messages.MIN_MAGNITUDE_USAGE,
        messages.STOPPED,
        messages.NOTHING_TO_STOP,
        messages.location_saved(view),
        messages.settings_updated(view),
        messages.subscription_list(view),
        render_alert(alert()),
        render_alert(alert(possible_duplicate_of=OCCURRED_AT)),
    ]


@pytest.mark.parametrize("text", _everything_the_bot_can_say())
def test_no_emoji_and_never_early_warning_wording(text: str) -> None:
    assert not _EMOJI.search(text)
    assert "peringatan dini" not in text.lower()
    for alarming in ("awas", "bahaya", "darurat", "segera"):
        assert alarming not in text.lower()


def test_start_explains_the_bot_and_includes_the_disclaimer() -> None:
    assert messages.DISCLAIMER in messages.START
    assert "BMKG" in messages.DISCLAIMER
    for command in ("/radius", "/minmag", "/list", "/stop"):
        assert command in messages.START
    assert "radius 200 km" in messages.START
    assert "magnitudo minimal 4.0" in messages.START
