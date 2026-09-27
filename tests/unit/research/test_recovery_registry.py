"""Every method in the recovery registry names recovery tests that exist (ADR 0043)."""

import ast

import pytest

from helpers.pipeline import REPO
from xq.research.recovery import RECOVERY_TESTS, recovery_tests


def _test_names(path: str) -> set[str]:
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


@pytest.mark.parametrize("method", sorted(RECOVERY_TESTS))
def test_every_named_recovery_test_exists(method: str) -> None:
    tests = recovery_tests(method)
    assert tests
    for node in tests:
        path, name = node.split("::")
        assert name.startswith("test_")
        assert name in _test_names(path), f"{node} does not exist"


def test_a_method_without_a_recovery_test_is_refused() -> None:
    with pytest.raises(KeyError, match="must not be used"):
        recovery_tests("hurst")
