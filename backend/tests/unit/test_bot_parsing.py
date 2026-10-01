from decimal import Decimal

import pytest

from app.notifications.bot import parse_command, parse_min_magnitude, parse_radius
from app.notifications.subscriptions import round_coordinate


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/start", ("start", "")),
        ("/radius 150", ("radius", "150")),
        ("  /radius   150  ", ("radius", "150")),
        ("/Radius@QuakeAlertBot 150", ("radius", "150")),
        ("/minmag 4,5", ("minmag", "4,5")),
        ("/start some-deep-link-payload", ("start", "some-deep-link-payload")),
        ("radius 150", None),
        ("hello", None),
        ("", None),
    ],
)
def test_parse_command(text: str, expected: tuple[str, str] | None) -> None:
    assert parse_command(text) == expected


@pytest.mark.parametrize(("argument", "expected"), [("10", 10), ("200", 200), ("1000", 1000)])
def test_parse_radius_accepts_whole_km_in_range(argument: str, expected: int) -> None:
    assert parse_radius(argument) == expected


@pytest.mark.parametrize(
    "argument", ["", "9", "1001", "0", "-50", "150.5", "150km", "1e3", "abc", "10000"]
)
def test_parse_radius_rejects(argument: str) -> None:
    with pytest.raises(ValueError):
        parse_radius(argument)


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("2", Decimal("2.0")),
        ("2.0", Decimal("2.0")),
        ("4.5", Decimal("4.5")),
        ("4,5", Decimal("4.5")),  # Indonesian decimal comma
        ("9", Decimal("9.0")),
        ("9.0", Decimal("9.0")),
    ],
)
def test_parse_min_magnitude_accepts(argument: str, expected: Decimal) -> None:
    value = parse_min_magnitude(argument)
    assert value == expected
    assert str(value) == str(expected)  # always one decimal, e.g. "2.0", not "2"


@pytest.mark.parametrize(
    "argument", ["", "1.9", "9.1", "10", "4.55", "-4", "4.", ".5", "four", "4.5.1", "NaN"]
)
def test_parse_min_magnitude_rejects(argument: str) -> None:
    with pytest.raises(ValueError):
        parse_min_magnitude(argument)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-6.208763, Decimal("-6.21")),
        (106.845599, Decimal("106.85")),
        (106.845, Decimal("106.85")),  # half up, from the decimal string, not binary float
        (-6.2049, Decimal("-6.20")),
        (0.004, Decimal("0.00")),
    ],
)
def test_round_coordinate_to_two_decimals(value: float, expected: Decimal) -> None:
    rounded = round_coordinate(value)
    assert rounded == expected
    assert rounded.as_tuple().exponent == -2
