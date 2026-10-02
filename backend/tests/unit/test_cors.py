import pytest

from app.core.config import Settings
from app.core.cors import cors_origins, covered


def settings(origins: str, environment: str = "development") -> Settings:
    return Settings(environment=environment, cors_allowed_origins=origins)  # type: ignore[arg-type]


def test_origins_are_split_and_trimmed() -> None:
    configured = settings(" https://quake.example.com , http://localhost:3000,, ")

    assert cors_origins(configured) == ["https://quake.example.com", "http://localhost:3000"]


def test_empty_means_no_cors() -> None:
    assert cors_origins(settings("")) == []


@pytest.mark.parametrize(
    "origin",
    [
        "quake.example.com",  # no scheme
        "ftp://quake.example.com",
        "https://quake.example.com/",  # trailing slash
        "https://quake.example.com/app",
        "https://quake.example.com?x=1",
        "https://user:pw@quake.example.com",
        "https://",
        "*.example.com",
    ],
)
def test_malformed_origins_are_refused(origin: str) -> None:
    with pytest.raises(ValueError, match="not an origin"):
        cors_origins(settings(origin))


def test_wildcard_is_refused_in_production_only() -> None:
    with pytest.raises(ValueError, match="production"):
        cors_origins(settings("https://quake.example.com,*", environment="production"))
    assert cors_origins(settings("*")) == ["*"]
    assert cors_origins(settings("https://quake.example.com", environment="production")) == [
        "https://quake.example.com"
    ]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/earthquakes", True),
        ("/v1/earthquakes/latest", True),
        ("/v1/earthquakes/0b6f2a4e-3c1d-4f7e-9a51-2d8c6e0f4b13", True),
        ("/v1/status", True),
        ("/v1/earthquakesX", False),
        ("/v1/statusX", False),
        ("/v1/subscriptions/webhook", False),
        ("/v1/telegram/webhook", False),
        ("/readyz", False),
        ("/docs", False),
    ],
)
def test_only_the_read_api_is_covered(path: str, expected: bool) -> None:
    assert covered(path) is expected
