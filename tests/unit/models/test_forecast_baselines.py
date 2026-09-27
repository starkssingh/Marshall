"""BASE-001: forecast baselines give known outputs fold by fold through the walk-forward runner."""

import math

import numpy as np
import pandas as pd
import pytest

from xq.core.errors import ConfigError
from xq.models.base import ModelConfig
from xq.models.baselines import (
    FORECAST_BASELINES,
    forecast_baseline,
    random_walk_columns,
)
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter
from xq.validation.walkforward import walk_forward

H = pd.Timedelta(hours=1)
N = 15 * 24
INDEX = pd.date_range("2024-01-01", periods=N, freq="h", tz="UTC", name="decision_time")
Y = pd.Series(np.random.default_rng(1).normal(0.001, 0.01, N), index=INDEX)
ENDS = pd.Series(INDEX + 2 * H, index=INDEX)
SPLITS = WalkForwardConfig.model_validate(
    {"min_train": "5D", "val_len": "1D", "test_len": "2D", "embargo": "2h"}
)
OPEN = pd.Series(100 + np.arange(N, dtype=float), index=INDEX)
FEATURES = pd.DataFrame({"ctx_1h_open": OPEN, "ctx_1h_close": OPEN * 1.01}, index=INDEX)


def run(name: str, features: list[str] | None = None, **params: str) -> pd.DataFrame:
    spec = forecast_baseline(name)
    config = ModelConfig(name=name, params=params, features=features or [])
    return walk_forward(FEATURES, Y, ENDS, spec, config, SPLITS, seed=3).predictions


def folds() -> list[tuple[np.ndarray, np.ndarray]]:
    """Training rows (labelled, purged) and test rows of every fold."""
    return [(f.train_idx, f.test_idx) for f in WalkForwardSplitter(SPLITS).split(INDEX, ENDS)]


def test_zero_return_forecasts_zero() -> None:
    predictions = run("zero_return")
    assert len(predictions) > 0
    assert (predictions["y_pred"] == 0.0).all()


def test_historical_mean_is_the_training_mean_of_each_fold() -> None:
    predictions = run("historical_mean")
    for train, test in folds():
        expected = Y.iloc[train].mean()
        np.testing.assert_allclose(predictions.loc[INDEX[test], "y_pred"], expected)
    # expanding windows: the mean moves as training grows
    assert predictions["y_pred"].nunique() == len(folds())


def test_climatology_is_the_training_frequency_of_positive_targets() -> None:
    predictions = run("climatology")
    for train, test in folds():
        expected = (Y.iloc[train] > 0).mean()
        np.testing.assert_allclose(predictions.loc[INDEX[test], "p_raw"], expected)
        assert 0 < expected < 1
    assert set(predictions["y_pred"].unique()) <= {0.0, 1.0}  # p > 0.5 as the class


def test_random_walk_repeats_the_latest_bar_return() -> None:
    predictions = run(
        "random_walk", ["ctx_1h_open", "ctx_1h_close"], open="ctx_1h_open", close="ctx_1h_close"
    )
    np.testing.assert_allclose(predictions["y_pred"], math.log(1.01))
    with pytest.raises(ConfigError, match="'open' and 'close'"):
        forecast_baseline("random_walk").factory({})


def test_baselines_are_fixed_benchmarks() -> None:
    assert set(FORECAST_BASELINES) == {
        "zero_return",
        "random_walk",
        "historical_mean",
        "climatology",
        "ar1",
    }
    assert FORECAST_BASELINES["climatology"].task == "classification"
    assert all(s.task == "regression" for n, s in FORECAST_BASELINES.items() if n != "climatology")
    with pytest.raises(ConfigError, match="unknown forecast baseline"):
        forecast_baseline("lightgbm")


def test_a_constant_forecast_needs_training_targets() -> None:
    estimator = forecast_baseline("historical_mean").factory({})
    with pytest.raises(ValueError, match="at least one training target"):
        estimator.fit(pd.DataFrame(), pd.Series(dtype=float), np.random.default_rng(0))


def test_random_walk_columns_follow_the_horizon() -> None:
    assert random_walk_columns("15m", "15m") == ("open", "close")
    assert random_walk_columns("1h", "15m") == ("ctx_1h_open", "ctx_1h_close")
    assert random_walk_columns("1d", "15m") == ("ctx_1d_open", "ctx_1d_close")


def test_ar1_is_an_ar1_of_bar_returns_whose_order_cannot_be_tuned() -> None:
    rng = np.random.default_rng(3)
    r = np.zeros(3000)
    for t in range(1, 3000):
        r[t] = 0.4 * r[t - 1] + rng.standard_normal() * 1e-3
    close = 2000.0 * np.exp(np.cumsum(r))
    x = pd.DataFrame({"open": close * np.exp(-r), "close": close})
    estimator = forecast_baseline("ar1").factory(
        {"open": "open", "close": "close", "horizon_bars": 2, "p": 5, "max_p": 9}
    )
    estimator.fit(x.iloc[:2000], pd.Series(np.zeros(2000)), np.random.default_rng(0))
    fitted = estimator.fitted
    assert (fitted.p, fitted.q) == (1, 0)
    assert fitted.ar[0] == pytest.approx(0.4, abs=0.05)
    forecast = estimator.predict(x.iloc[2000:])
    phi, mu = fitted.ar[0], fitted.mu
    expected = 2 * mu + (phi + phi**2) * (r[2000:] - mu)
    np.testing.assert_allclose(forecast, expected, rtol=1e-9, atol=1e-15)
    assert FORECAST_BASELINES["ar1"].task == "regression"
