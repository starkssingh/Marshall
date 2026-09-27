"""Recovery tests every Phase 5 and Phase 6 method passes before anything uses it (ADR 0043).

The plan requires every statistical and volatility method to recover a known answer on a simulated
process before it runs on gold. `RECOVERY_TESTS` names, per method, the tests (pytest node ids,
relative to the repository root) that prove it; a unit test checks that every named test exists,
and the verdict report (STAT-008) cites them in each method's evidence. A method without an entry
here must not be used by a report, a board or the sigma-hat selection.
"""

from __future__ import annotations

from collections.abc import Mapping

_STATIONARITY = "tests/unit/research/test_stats_stationarity.py"

RECOVERY_TESTS: Mapping[str, tuple[str, ...]] = {
    "ADF": (
        f"{_STATIONARITY}::test_random_walk_adf_does_not_reject_and_the_verdict_is_unit_root",
        f"{_STATIONARITY}::test_adf_size_on_random_walks_is_near_its_level",
        f"{_STATIONARITY}::test_stationary_ar1_is_stationary_and_its_returns_too",
    ),
    "PP": (
        f"{_STATIONARITY}::test_random_walk_adf_does_not_reject_and_the_verdict_is_unit_root",
        f"{_STATIONARITY}::test_stationary_ar1_is_stationary_and_its_returns_too",
    ),
    "KPSS": (
        f"{_STATIONARITY}::test_random_walk_adf_does_not_reject_and_the_verdict_is_unit_root",
        f"{_STATIONARITY}::test_stationary_ar1_is_stationary_and_its_returns_too",
    ),
    "ZA": (f"{_STATIONARITY}::test_zivot_andrews_finds_a_level_shift_near_its_date",),
}


def recovery_tests(method: str) -> tuple[str, ...]:
    """The recovery tests of `method`.

    Raises:
        KeyError: if the method has no recovery test (it must not be used).
    """
    try:
        return RECOVERY_TESTS[method]
    except KeyError:
        raise KeyError(
            f"{method!r} has no recovery test on a simulated process; it must not be used"
        ) from None
