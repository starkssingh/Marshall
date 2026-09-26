"""Suite-wide pytest configuration.

Tests are marked by the directory they live in, so `-m unit` or `-m leakage` selects the right set
without relying on every test remembering its marker.
"""

import os
from pathlib import Path

import pytest

_DIRECTORY_MARKERS = ("unit", "integration", "property", "leakage")
_TESTS_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply the marker named after each test's top-level directory under `tests/`."""
    for item in items:
        relative = item.path.relative_to(_TESTS_ROOT)
        top = relative.parts[0] if len(relative.parts) > 1 else ""
        if top in _DIRECTORY_MARKERS:
            item.add_marker(getattr(pytest.mark, top))


@pytest.fixture(autouse=True)
def _isolate_xq_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove XQ_* variables so a developer's shell cannot change test outcomes."""
    for name in list(os.environ):
        if name.upper().startswith("XQ_"):
            monkeypatch.delenv(name)
