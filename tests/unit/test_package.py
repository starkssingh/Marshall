"""Smoke tests for the installed package."""

import xq


def test_package_imports_and_exposes_version() -> None:
    assert isinstance(xq.__version__, str)
    assert xq.__version__ != ""
