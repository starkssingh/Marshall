"""Recovery tests every Phase 5, 6, 16 and 17 method passes before anything uses it (ADR 0043,
ADR 0054).

The plan requires every statistical and volatility method to recover a known answer on a simulated
process before it runs on gold; Sprint 9's validation and robustness methods (VAL-003, VAL-004,
VAL-006, ROB-001 ... ROB-007) are held to the same rule on simulated strategies with known truth —
noise-only families, a single-point optimum on noise, a genuine edge. `RECOVERY_TESTS` names, per
method, the tests (pytest node ids, relative to the repository root) that prove it; a unit test
checks that every named test exists, and the verdict report (STAT-008) cites them in each method's
evidence. A method without an entry here must not be used by a report, a board, the sigma-hat
selection, a validation report or a robustness report.
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
_PBO = "tests/unit/validation/test_pbo.py"
_SPA = "tests/unit/validation/test_spa.py"
_MULTIPLE = "tests/unit/validation/test_multiple_testing.py"
_PERTURB = "tests/unit/robustness/test_perturb.py"
_COST_STRESS = "tests/unit/robustness/test_costs_stress.py"
_BOOTSTRAP = "tests/unit/robustness/test_bootstrap.py"
_SLICING = "tests/integration/robustness/test_slicing.py"

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
    "PBO": (
        f"{_PBO}::test_a_hand_computed_case",
        f"{_PBO}::test_noise_families_have_a_pbo_near_one_half",
        f"{_PBO}::test_a_graded_genuine_edge_passes",
        f"{_PBO}::test_a_single_point_optimum_on_noise_fails",
    ),
    "reality_check_spa_romano_wolf": (
        f"{_SPA}::test_a_noise_only_family_is_rejected_at_about_the_nominal_rate",
        f"{_SPA}::test_volatility_clustering_keeps_the_size_near_nominal",
        f"{_SPA}::test_a_genuine_edge_is_detected_and_its_survivors_named",
        f"{_SPA}::test_spa_keeps_its_power_when_poor_strategies_join_the_family",
    ),
    "holm_bh": (
        f"{_MULTIPLE}::test_hand_computed_reference_values",
        f"{_MULTIPLE}::test_the_adjustments_match_statsmodels",
        f"{_MULTIPLE}::test_holm_controls_the_family_wise_error_and_bh_the_false_discovery_rate",
    ),
    "parameter_perturbation": (
        f"{_PERTURB}::test_a_single_point_optimum_on_noise_fails_the_neighbourhood_gate",
        f"{_PERTURB}::test_a_genuine_trend_edge_passes_the_neighbourhood_gate",
        f"{_PERTURB}::test_the_designs_evaluate_the_points_they_state",
    ),
    "cost_stress": (
        f"{_COST_STRESS}::test_the_r2_scenario_passes_exactly_when_the_gross_edge_covers_the_stressed_costs",
        f"{_COST_STRESS}::test_a_thin_edge_profitable_at_baseline_fails_and_a_thick_one_passes",
        f"{_COST_STRESS}::test_the_break_even_multiplier_leaves_no_net_pnl",
        f"{_COST_STRESS}::test_latency_eats_a_signal_priced_in_over_seconds_in_proportion_to_the_delay",
    ),
    "returns_bootstrap": (
        f"{_BOOTSTRAP}::test_sharpe_and_cagr_intervals_cover_at_about_the_nominal_rate",
        f"{_BOOTSTRAP}::test_blocks_keep_the_coverage_under_serial_correlation",
        f"{_BOOTSTRAP}::test_the_drawdown_interval_brackets_the_true_drawdown_median",
    ),
    "trade_permutation": (
        f"{_BOOTSTRAP}::test_unordered_trades_sit_anywhere_in_the_permutation_distribution",
        f"{_BOOTSTRAP}::test_clustered_losses_are_at_the_top_of_the_permutation_distribution",
    ),
    "pre_registered_slicing": (
        f"{_SLICING}::test_slices_come_from_the_locked_version_the_run_tested",
        f"{_SLICING}::test_an_edge_earned_in_one_year_fails_the_single_year_gate",
        f"{_SLICING}::test_volatility_terciles_find_an_edge_that_lives_in_high_volatility",
        f"{_SLICING}::test_sessions_follow_dst_and_name_the_overlap",
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
