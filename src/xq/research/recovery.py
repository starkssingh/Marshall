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
_DEPENDENCE = "tests/unit/research/test_stats_dependence.py"
_VARIANCE_RATIO = "tests/unit/research/test_stats_variance_ratio.py"

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
    "LB": (
        f"{_DEPENDENCE}::test_ljung_box_and_arch_lm_match_statsmodels",
        f"{_DEPENDENCE}::test_garch_squared_returns_reject_and_arch_lm_rejects",
        f"{_DEPENDENCE}::test_iid_returns_show_no_arch_effects",
    ),
    "Q*": (
        f"{_DEPENDENCE}::test_robust_portmanteau_keeps_its_size_under_garch_where_ljung_box_does_not",
        f"{_DEPENDENCE}::test_ar1_returns_are_detected_by_the_robust_test",
    ),
    "ARCH-LM": (
        f"{_DEPENDENCE}::test_ljung_box_and_arch_lm_match_statsmodels",
        f"{_DEPENDENCE}::test_garch_squared_returns_reject_and_arch_lm_rejects",
    ),
    "VR": (
        f"{_VARIANCE_RATIO}::test_ou_increments_have_variance_ratios_below_one_matching_theory",
        f"{_VARIANCE_RATIO}::test_random_walk_increments_do_not_reject",
    ),
    "Chow-Denning": (
        f"{_VARIANCE_RATIO}::test_ou_increments_have_variance_ratios_below_one_matching_theory",
        f"{_VARIANCE_RATIO}::test_chow_denning_size_is_controlled_on_garch_returns",
    ),
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
