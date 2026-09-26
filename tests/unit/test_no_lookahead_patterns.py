"""DS-003 lint: look-ahead constructs are banned from library code.

Centered windows and backward fills read the future. Negative shifts do too, and are only allowed
in target code (``src/xq/targets/``), which is forward-looking by definition and bounded by
``label_end``. The leakage harness (DS-006) catches these at runtime; this test stops them from
being written at all.
"""

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "xq"
TARGETS = SRC / "targets"

BANNED = {
    "centered window": re.compile(r"center\s*=\s*True"),
    "backward fill": re.compile(r"\.bfill\(|backfill|method\s*=\s*['\"]bfill['\"]"),
}
NEGATIVE_SHIFT = re.compile(r"\.shift\(\s*-")


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


@pytest.mark.parametrize("name", sorted(BANNED))
def test_banned_patterns_are_absent(name: str) -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in _sources()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if BANNED[name].search(line)
    ]
    assert offenders == [], f"{name} found in library code: {offenders}"


def test_negative_shifts_only_in_target_code() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in _sources()
        if TARGETS not in path.parents
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if NEGATIVE_SHIFT.search(line)
    ]
    assert offenders == [], f"negative shift outside target code: {offenders}"


def test_the_lint_detects_what_it_bans() -> None:
    assert BANNED["centered window"].search("x.rolling(5, center=True).mean()")
    assert BANNED["backward fill"].search("frame.bfill()")
    assert BANNED["backward fill"].search("s.fillna(method='bfill')")
    assert NEGATIVE_SHIFT.search("s.shift(-1)")
    assert not NEGATIVE_SHIFT.search("s.shift(1)")
