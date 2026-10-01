from pathlib import Path

from app.core.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def test_env_example_documents_every_setting() -> None:
    documented = {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    expected = {name.upper() for name in Settings.model_fields}

    assert expected - documented == set(), "add these to backend/.env.example"
