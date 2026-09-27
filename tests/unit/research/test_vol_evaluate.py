"""VOL-005: HAR and EWMA (and every other model) are evaluated with QLIKE on identical folds and
targets; each model is fitted on its fold's training periods only; on a simulated GARCH process the
true conditional variance ranks first, stays in the 90 % MCS and passes Mincer-Zarnowitz while a
biased forecast fails; regime cut-offs come from training rows only."""

import numpy as np
import pandas as pd
import pytest
from statsmodels.regression.linear_model import OLS

from helpers.simulate import garch_periods
from xq.core.config import GarchSpec, VolEvaluationConfig
from xq.models.volatility import VolForecaster
from xq.research.volatility.benchmarks import Ewma, Har, RollingRV
from xq.research.volatility.evaluate import (
    evaluate_forecasters,
    mincer_zarnowitz,
    realized_target,
    regime_labels,
)
from xq.research.volatility.garch import GarchForecaster
from xq.validation.forecast_eval import qlike
from xq.validation.splitters import WalkForwardConfig

EVAL = VolEvaluationConfig(
    mcs_alpha=0.10,
    mcs_n_boot=500,
    mcs_mean_block=5.0,
    dm_alpha=0.05,
    dm_reference="har",
    regime_window=22,
    regime_quantiles=[1 / 3, 2 / 3],
)
SPLITTER = WalkForwardConfig(mode="expanding", min_train="1000D", test_len="365D")


class Oracle(VolForecaster):
    """The true next-period GARCH variance (known at t for a GARCH), times a bias factor."""

    def __init__(self, bias: float = 1.0) -> None:
        self.bias = bias
        self.name = "oracle" if bias == 1.0 else f"biased_{bias:g}"

    def _fit(self, periods: pd.DataFrame) -> None:
        """Nothing to learn."""

    def _predict(self, periods: pd.DataFrame, horizon: int) -> np.ndarray:
        assert horizon == 1
        return self.bias * periods["h_next"].to_numpy()


def _periods(n: int = 3000, seed: int = 1) -> pd.DataFrame:
    periods, h = garch_periods(n, omega=0.05, alpha=0.08, beta=0.90, seed=seed)
    h_next = np.r_[h[1:], np.nan]  # h_{t+1} is a function of data up to t in a GARCH
    return periods.assign(h_next=h_next)


def test_har_and_ewma_are_evaluated_with_qlike_on_identical_folds() -> None:
    periods = _periods()
    board = evaluate_forecasters(
        periods,
        {"har": lambda: Har([1, 5, 22], 0.01), "ewma_0.94": lambda: Ewma(0.94)},
        1,
        SPLITTER,
        EVAL,
        default="ewma_0.94",
        seed=3,
    )
    assert len(board.fold_ids) >= 4
    forecasts = board.forecasts
    assert forecasts[["har", "ewma_0.94"]].notna().all().all()
    target, _ = realized_target(periods, 1)
    pd.testing.assert_series_equal(
        forecasts["target"], target.loc[forecasts.index], check_names=False
    )
    for name in ("har", "ewma_0.94"):
        row = board.metrics.set_index("model").loc[name]
        assert row["qlike"] == pytest.approx(qlike(forecasts["target"], forecasts[name]))
        assert row["n"] == len(forecasts)
    per_fold = board.fold_metrics.pivot(index="fold_id", columns="model", values="n")
    assert (per_fold["har"] == per_fold["ewma_0.94"]).all()
    assert list(per_fold.index) == board.fold_ids
    # each model was fitted on its fold's training periods only: refit the first fold by hand
    first = forecasts.loc[forecasts["fold_id"] == board.fold_ids[0]]
    train = periods.loc[periods.index < first.index[0] - pd.Timedelta(days=1)]
    manual = Ewma(0.94).fit(train).predict_variance(periods.loc[: first.index[-1]], 1)
    np.testing.assert_allclose(first["ewma_0.94"], manual.loc[first.index])


def test_the_true_variance_ranks_first_and_a_biased_forecast_is_rejected() -> None:
    periods = _periods()
    board = evaluate_forecasters(
        periods,
        {
            "har": lambda: Har([1, 5, 22], 0.01),
            "ewma_0.94": lambda: Ewma(0.94),
            "rolling_22": lambda: RollingRV(22),
            "garch_normal": lambda: GarchForecaster(
                "garch_normal", GarchSpec(vol="GARCH"), "normal"
            ),
            "oracle": Oracle,
            "biased_2": lambda: Oracle(2.0),
        },
        1,
        SPLITTER,
        EVAL,
        default="ewma_0.94",
        seed=4,
    )
    metrics = board.metrics.set_index("model")
    assert metrics["qlike"].idxmin() == "oracle"
    assert metrics.loc["oracle", "in_mcs"]
    assert not metrics.loc["biased_2", "in_mcs"]
    assert metrics.loc["oracle", "dm_vs_ref_p_less"] < 0.05
    assert metrics.loc["oracle", "mz_beta"] == pytest.approx(1.0, abs=0.1)
    assert metrics.loc["oracle", "mz_p"] > 0.05
    assert metrics.loc["biased_2", "mz_p"] < 0.05
    # an estimated GARCH is not the truth: with persistence 0.98 its unconditional level is
    # imprecise, so over ~2,000 test days the MCS may separate it from the true variance
    assert metrics.loc["garch_normal", "qlike"] > metrics.loc["oracle", "qlike"]
    assert "oracle" in board.mcs.included


def test_mincer_zarnowitz_matches_ols_with_white_errors() -> None:
    rng = np.random.default_rng(5)
    f = rng.gamma(2.0, 1.0, 800)
    y = 0.2 + 0.9 * f + rng.standard_normal(800) * f
    ours = mincer_zarnowitz(y, f, lags=0)
    result = OLS(y, np.column_stack([np.ones(800), f])).fit(cov_type="HC0")
    assert ours["mz_alpha"] == pytest.approx(result.params[0])
    assert ours["mz_beta"] == pytest.approx(result.params[1])
    theta = result.params - np.array([0.0, 1.0])
    wald = theta @ np.linalg.solve(result.cov_params(), theta)
    assert ours["mz_wald"] == pytest.approx(wald)
    assert ours["mz_r2"] == pytest.approx(result.rsquared)


def test_regime_cut_offs_come_from_training_rows_only() -> None:
    periods = _periods(1000)
    train = np.arange(600)
    labels = regime_labels(periods, train, 22, [1 / 3, 2 / 3])
    shocked = periods.copy()
    shocked.iloc[700:, shocked.columns.get_loc("rv")] *= 100.0
    relabelled = regime_labels(shocked, train, 22, [1 / 3, 2 / 3])
    np.testing.assert_array_equal(labels[:700], relabelled[:700])
    assert set(relabelled[722:]) == {"v2"}
    assert all(label is None for label in labels[:21])


def test_rows_are_dropped_for_every_model_and_slices_are_reported() -> None:
    periods = _periods(2000)

    class Gappy(Ewma):
        def _predict(self, periods: pd.DataFrame, horizon: int) -> np.ndarray:
            values = super()._predict(periods, horizon)
            values[::7] = np.nan
            return values

    sessions = pd.Series(
        np.where(periods.index.dayofweek < 2, "early", "late"), index=periods.index
    )
    board = evaluate_forecasters(
        periods,
        {"har": lambda: Har([1, 5, 22], 0.01), "ewma_0.94": lambda: Gappy(0.94)},
        1,
        SPLITTER,
        EVAL,
        default="ewma_0.94",
        seed=5,
        sessions=sessions,
    )
    assert board.n_dropped > 0
    assert board.metrics["n"].nunique() == 1
    assert set(board.slices["slice"]) == {"session", "vol_regime"}
    assert set(board.slices.loc[board.slices["slice"] == "session", "label"]) == {"early", "late"}


def test_bad_boards_are_refused() -> None:
    periods = _periods(2000)
    with pytest.raises(ValueError, match="needs 'har'"):
        evaluate_forecasters(
            periods,
            {"ewma_0.94": lambda: Ewma(0.94)},
            1,
            SPLITTER,
            EVAL,
            default="ewma_0.94",
            seed=1,
        )

    class Negative(Ewma):
        def _predict(self, periods: pd.DataFrame, horizon: int) -> np.ndarray:
            return -super()._predict(periods, horizon)

    with pytest.raises(ValueError, match="non-positive"):
        evaluate_forecasters(
            periods,
            {"har": lambda: Har([1, 5, 22], 0.01), "ewma_0.94": lambda: Negative(0.94)},
            1,
            SPLITTER,
            EVAL,
            default="ewma_0.94",
            seed=1,
        )
    with pytest.raises(ValueError, match="no fold"):
        evaluate_forecasters(
            periods.iloc[:100],
            {"har": lambda: Har([1, 5, 22], 0.01), "ewma_0.94": lambda: Ewma(0.94)},
            1,
            SPLITTER,
            EVAL,
            default="ewma_0.94",
            seed=1,
        )
