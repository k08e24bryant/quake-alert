from pathlib import Path

import pytest

INTEGRATION_DIR = Path(__file__).parent / "integration"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Everything under tests/integration needs real services; mark it so `-m "not integration"`
    # gives a fast, dependency-free run.
    for item in items:
        if INTEGRATION_DIR in Path(item.path).parents:
            item.add_marker(pytest.mark.integration)
