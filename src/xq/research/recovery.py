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
_ARIMA = "tests/unit/research/test_stats_arima.py"
_ESTIMATORS = "tests/unit/research/test_vol_estimators.py"
_REALIZED = "tests/unit/research/test_vol_realized.py"
_BENCHMARKS = "tests/unit/research/test_vol_benchmarks.py"
_GARCH = "tests/unit/research/test_vol_garch.py"
_EVALUATE = "tests/unit/research/test_vol_evaluate.py"
_SELECTION = "tests/unit/models/test_volatility_selection.py"

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
    "ARMA": (
        f"{_ARIMA}::test_ar1_with_phi_one_half_is_recovered",
        f"{_ARIMA}::test_arma11_parameters_are_recovered",
        f"{_ARIMA}::test_aic_selects_the_order_of_an_ar2",
        f"{_ARIMA}::test_one_step_forecasts_match_statsmodels_after_burn_in",
        f"{_ARIMA}::test_forecasts_are_causal",
    ),
    "DM": (
        f"{_ARIMA}::test_walk_forward_ar1_beats_the_benchmarks_on_identical_folds",
        f"{_ARIMA}::test_iid_returns_give_no_useful_evidence",
    ),
    "range_estimators": (
        f"{_ESTIMATORS}::test_estimators_match_hand_computations",
        f"{_ESTIMATORS}::test_estimators_recover_the_variance_of_a_brownian_path",
        f"{_ESTIMATORS}::test_estimators_are_trailing",
    ),
    "wilder_atr": (f"{_ESTIMATORS}::test_wilder_atr_by_hand",),
    "realized_measures": (
        f"{_REALIZED}::test_measures_match_hand_computations",
        f"{_REALIZED}::test_hourly_rv_adds_up_to_daily_rv",
        f"{_REALIZED}::test_bipower_separates_jumps_from_the_diffusion",
    ),
    "diurnal_factor": (
        f"{_REALIZED}::test_the_diurnal_factor_recovers_an_injected_pattern",
        f"{_REALIZED}::test_the_diurnal_factor_is_fitted_on_training_rows_only",
    ),
    "rolling_rv": (
        f"{_BENCHMARKS}::test_rolling_rv_by_hand",
        f"{_BENCHMARKS}::test_benchmarks_are_causal",
    ),
    "ewma": (
        f"{_BENCHMARKS}::test_ewma_by_hand",
        f"{_BENCHMARKS}::test_benchmarks_are_causal",
    ),
    "har": (
        f"{_BENCHMARKS}::test_har_recovers_its_coefficients",
        f"{_BENCHMARKS}::test_har_is_fitted_on_training_rows_whose_future_is_training",
        f"{_BENCHMARKS}::test_benchmarks_are_causal",
    ),
    "deseasonalized": (
        f"{_BENCHMARKS}::test_the_diurnal_adjustment_is_fitted_on_training_periods_only",
        f"{_BENCHMARKS}::test_the_diurnal_adjustment_scales_forecasts_by_the_next_hours",
    ),
    "garch": (
        f"{_GARCH}::test_garch11_parameters_are_recovered",
        f"{_GARCH}::test_gjr_leverage_is_recovered",
        f"{_GARCH}::test_egarch_parameters_are_recovered",
        f"{_GARCH}::test_student_t_degrees_of_freedom_are_recovered",
        f"{_GARCH}::test_forecasts_follow_the_garch_recursion_and_its_closed_form",
        f"{_GARCH}::test_forecasts_are_causal_and_reproducible",
    ),
    "vol_evaluation": (
        f"{_EVALUATE}::test_har_and_ewma_are_evaluated_with_qlike_on_identical_folds",
        f"{_EVALUATE}::test_the_true_variance_ranks_first_and_a_biased_forecast_is_rejected",
        f"{_EVALUATE}::test_mincer_zarnowitz_matches_ols_with_white_errors",
        f"{_EVALUATE}::test_regime_cut_offs_come_from_training_rows_only",
    ),
    "vol_selection": (
        f"{_SELECTION}::test_nothing_beats_the_default_so_ewma_stays",
        f"{_SELECTION}::test_the_best_eligible_model_is_selected",
        f"{_SELECTION}::test_p_values_are_holm_adjusted_across_all_challengers",
        f"{_SELECTION}::test_twelve_null_challengers_promote_in_at_most_about_five_percent_of_samples",
        f"{_SELECTION}::test_ewma_is_kept_end_to_end_when_it_is_the_true_model",
        f"{_SELECTION}::test_sigma_is_served_per_fold_from_training_periods_only",
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
