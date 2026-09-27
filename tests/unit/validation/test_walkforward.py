"""WF-002: the walk-forward runner fits, selects and predicts fold by fold, deterministically."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from helpers.models import FREQUENCY, LAST, MEAN, NOISY, OLS
from xq.core.errors import NaiveTimestampError
from xq.models.base import ModelConfig, ModelSpec
from xq.validation.splitters import WalkForwardConfig
from xq.validation.walkforward import PREDICTION_COLUMNS, WalkForwardOutput, walk_forward

H = pd.Timedelta(hours=1)


def hourly(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC", name="decision_time")


def noise(n: int, seed: int = 0, mean: float = 0.0) -> pd.Series:
    return pd.Series(np.random.default_rng(seed).normal(mean, 1, n), index=hourly(n))


def splits(**params: str) -> WalkForwardConfig:
    return WalkForwardConfig.model_validate({"min_train": "5D", "test_len": "2D", **params})


def run(
    y: pd.Series,
    config: ModelConfig | None = None,
    *,
    spec: ModelSpec = MEAN,
    ends: pd.Series | None = None,
    features: pd.DataFrame | None = None,
    splitter: WalkForwardConfig | None = None,
    **kwargs: object,
) -> WalkForwardOutput:
    index = pd.DatetimeIndex(y.index)
    return walk_forward(
        features if features is not None else pd.DataFrame(index=index),
        y,
        ends if ends is not None else pd.Series(index + 2 * H, index=index),
        spec,
        config or ModelConfig(name=spec.name),
        splitter or splits(),
        seed=7,
        **kwargs,  # type: ignore[arg-type]
    )


def test_every_test_row_is_predicted_once_after_its_training_information() -> None:
    y = noise(20 * 24)
    out = run(y, splitter=splits(embargo="3h"))
    predictions = out.predictions
    assert list(predictions.columns) == list(PREDICTION_COLUMNS)
    assert predictions.index.is_unique
    assert predictions.index.is_monotonic_increasing
    assert predictions.index[0] == y.index[0] + pd.Timedelta(days=5)
    assert len(predictions) == len(y) - 5 * 24
    assert (predictions.index > predictions["train_end"] + pd.Timedelta(hours=3)).all()
    first = out.folds[0]
    assert first.n_train == 5 * 24 - 5  # labels t + 2h purged 3 hours before the test start
    assert predictions.loc[predictions["fold_id"] == "f000", "y_pred"].iloc[0] == pytest.approx(
        y.iloc[: first.n_train].mean()
    )
    np.testing.assert_allclose(predictions["y_true"], y.iloc[5 * 24 :])
    assert predictions["p_raw"].isna().all()
    assert set(first.metrics) == {"n", "mse", "mae", "hit_rate", "mean_pred"}


def test_the_grid_is_selected_on_validation_rows() -> None:
    y = noise(20 * 24, mean=1.0)
    config = ModelConfig(name="mean", grid={"shrink": [0.0, 0.5, 1.0]})
    out = run(y, config, splitter=splits(val_len="2D"))
    assert all(f.selected == {"shrink": 1.0} for f in out.folds)
    assert all(len(f.validation_losses) == 3 for f in out.folds)
    assert out.folds[0].validation_losses[0] > out.folds[0].validation_losses[2]
    without_validation = run(y, config)
    assert all(f.selected == {"shrink": 0.0} for f in without_validation.folds)  # the first
    assert all(f.validation_losses == [] for f in without_validation.folds)


def test_serial_and_parallel_runs_give_identical_results() -> None:
    y = noise(20 * 24)
    serial = run(y, spec=NOISY)
    parallel = run(y, spec=NOISY, n_jobs=2)
    pd.testing.assert_frame_equal(serial.predictions, parallel.predictions)
    assert serial.folds == parallel.folds
    again = run(y, spec=NOISY)
    pd.testing.assert_frame_equal(serial.predictions, again.predictions)


def test_cached_folds_return_what_the_computation_would(tmp_path: Path) -> None:
    y = noise(20 * 24)
    first = run(y, spec=NOISY, cache_dir=tmp_path)
    assert not any(f.cached for f in first.folds)
    second = run(y, spec=NOISY, cache_dir=tmp_path)
    assert all(f.cached for f in second.folds)
    pd.testing.assert_frame_equal(first.predictions, second.predictions, check_freq=False)
    changed = y.copy()
    changed.iloc[-1] += 1.0  # only the last fold reads this row
    partly = run(changed, spec=NOISY, cache_dir=tmp_path)
    assert [f.cached for f in partly.folds] == [True] * (len(partly.folds) - 1) + [False]
    other = run(
        y, ModelConfig(name="noisy_mean", params={"shrink": 0.5}), spec=NOISY, cache_dir=tmp_path
    )
    assert not any(f.cached for f in other.folds)


def test_an_ar1_signal_gives_the_analytically_expected_hit_rate() -> None:
    """y_t = 0.5 y_(t-1) + e_t; forecasting from y_(t-1), the sign is right with probability
    1/2 + arcsin(0.5)/pi = 2/3 for a Gaussian AR(1)."""
    n, phi = 6000, 0.5
    rng = np.random.default_rng(3)
    values = np.zeros(n + 1)
    for i in range(1, n + 1):
        values[i] = phi * values[i - 1] + rng.normal()
    index = hourly(n)
    y = pd.Series(values[1:], index=index)  # realized one step after its decision time
    features = pd.DataFrame({"previous": values[:-1]}, index=index)  # known at decision time
    ends = pd.Series(index + H, index=index)
    config = ModelConfig(name="ols", features=["previous"])
    out = run(
        y,
        config,
        spec=OLS,
        ends=ends,
        features=features,
        splitter=splits(min_train="20D", test_len="20D"),
    )
    hits = np.sign(out.predictions["y_pred"]) == np.sign(out.predictions["y_true"])
    assert hits.mean() == pytest.approx(0.5 + np.arcsin(phi) / np.pi, abs=0.025)


def test_overlapping_labels_without_signal_are_at_chance_under_purging() -> None:
    """Labels are sums of the next 24 independent increments, so neighbouring labels overlap
    almost entirely. A model that memorizes the latest training label looks highly predictive if
    label_end is misreported as the decision time, and is at chance once labels are purged."""
    n, horizon = 1500, 24
    steps = np.random.default_rng(5).normal(size=n + horizon)
    index = hourly(n)
    y = pd.Series([steps[i + 1 : i + 1 + horizon].sum() for i in range(n)], index=index)
    one_by_one = splits(min_train="5D", test_len="1h")
    purged = run(
        y, spec=LAST, ends=pd.Series(index + horizon * H, index=index), splitter=one_by_one
    )
    leaky = run(y, spec=LAST, ends=pd.Series(index, index=index), splitter=one_by_one)

    def hit_rate(predictions: pd.DataFrame) -> float:
        return float((np.sign(predictions["y_pred"]) == np.sign(predictions["y_true"])).mean())

    assert len(purged.predictions) > 1000
    assert hit_rate(leaky.predictions) > 0.8
    assert hit_rate(purged.predictions) == pytest.approx(0.5, abs=0.06)


def test_classification_forecasts_probabilities_of_a_positive_target() -> None:
    y = noise(20 * 24, mean=0.3)
    out = run(y, spec=FREQUENCY)
    predictions = out.predictions
    assert set(predictions["y_true"].unique()) <= {0.0, 1.0}
    assert predictions["p_raw"].between(0, 1).all()
    np.testing.assert_array_equal(predictions["y_pred"], (predictions["p_raw"] > 0.5).astype(float))
    assert set(out.folds[0].metrics) == {"n", "log_loss", "brier", "accuracy"}


def test_inputs_are_validated() -> None:
    y = noise(10 * 24)
    with pytest.raises(NaiveTimestampError):
        run(y.tz_localize(None))
    with pytest.raises(KeyError, match="not in the dataset"):
        run(y, ModelConfig(name="ols", features=["missing"]), spec=OLS)
    with pytest.raises(ValueError, match="one index"):
        run(y, ends=pd.Series(y.index[:-1] + H, index=y.index[:-1]))
    with pytest.raises(ValueError, match="n_jobs"):
        run(y, n_jobs=0)
