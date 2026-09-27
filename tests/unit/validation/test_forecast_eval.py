"""BASE-006: forecast metrics against published examples and independent scipy computations."""

import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import mannwhitneyu

from xq.validation.forecast_eval import (
    auc,
    brier,
    classification_metrics,
    ece,
    log_loss,
    loss_series,
    mae,
    mse,
    qlike,
    regression_metrics,
    reliability_curve,
)


def test_log_loss_and_brier_match_the_scikit_learn_documentation_examples() -> None:
    # sklearn.metrics.log_loss(["spam", "ham", "ham", "spam"], [[.1, .9], [.9, .1], [.8, .2],
    # [.35, .65]]) == 0.21616...; brier_score_loss([0, 1, 1, 0], [.1, .9, .8, .3]) == 0.0375
    assert log_loss([1, 0, 0, 1], [0.9, 0.1, 0.2, 0.65]) == pytest.approx(0.216161, abs=1e-6)
    assert brier([0, 1, 1, 0], [0.1, 0.9, 0.8, 0.3]) == pytest.approx(0.0375)


def test_auc_matches_the_documentation_example_and_mann_whitney() -> None:
    # sklearn.metrics.roc_auc_score([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == 0.75
    assert auc([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == pytest.approx(0.75)
    rng = np.random.default_rng(2)
    y = rng.integers(0, 2, 500)
    p = np.round(rng.random(500) * 0.5 + 0.3 * y, 2)  # rounded, so there are ties
    u = mannwhitneyu(p[y == 1], p[y == 0]).statistic
    assert auc(y, p) == pytest.approx(u / ((y == 1).sum() * (y == 0).sum()))
    assert math.isnan(auc([1, 1], [0.2, 0.3]))


def test_reliability_curve_and_ece_by_hand() -> None:
    y = [0, 0, 1, 1, 1, 0]
    p = [0.05, 0.15, 0.95, 0.85, 0.9, 1.0]  # 1.0 falls in the last bin
    table = reliability_curve(y, p, n_bins=2)
    assert table["bin"].tolist() == [0, 1]
    assert table["count"].tolist() == [2, 4]
    assert table["p_mean"].tolist() == pytest.approx([0.1, 0.925])
    assert table["y_rate"].tolist() == pytest.approx([0.0, 0.75])
    expected = 2 / 6 * 0.1 + 4 / 6 * abs(0.925 - 0.75)
    assert ece(y, p, n_bins=2) == pytest.approx(expected)
    assert ece([0, 1], [0.0, 1.0]) == 0.0  # perfectly calibrated and sharp


def test_point_and_variance_losses() -> None:
    assert mse([1.0, 2.0, 3.0], [1.0, 1.0, 5.0]) == pytest.approx(5 / 3)
    assert mae([1.0, 2.0, 3.0], [1.0, 1.0, 5.0]) == pytest.approx(1.0)
    assert qlike([1.0, 2.0], [1.0, 2.0]) == 0.0
    ratio = 2.0 / 1.0
    assert qlike([2.0], [1.0]) == pytest.approx(ratio - math.log(ratio) - 1)
    assert qlike([1.0], [2.0]) > 0  # over- and under-prediction both cost
    with pytest.raises(ValueError, match="positive variance"):
        qlike([1.0], [0.0])


def test_loss_series_is_aligned_per_observation() -> None:
    index = pd.date_range("2024-03-12", periods=3, freq="h", tz="UTC")
    actual = pd.Series([1.0, 0.0, 1.0], index=index)
    forecast = pd.Series([0.8, 0.3, 0.4], index=index)
    losses = loss_series(actual, forecast, "brier")
    assert losses.index.equals(index)
    np.testing.assert_allclose(losses, [0.04, 0.09, 0.36])
    assert loss_series(actual, forecast, "log").mean() == pytest.approx(log_loss(actual, forecast))
    with pytest.raises(ValueError, match="one index"):
        loss_series(actual, forecast.iloc[:2], "squared")
    with pytest.raises(ValueError, match="0 or 1"):
        loss_series(actual + 0.5, forecast, "log")
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        loss_series(actual, forecast + 1, "brier")


def test_summaries() -> None:
    c = classification_metrics([0, 1, 1, 0], [0.1, 0.9, 0.8, 0.3])
    assert set(c) == {"log_loss", "brier", "ece", "auc", "accuracy"}
    assert (c["auc"], c["accuracy"]) == (1.0, 1.0)
    r = regression_metrics([0.01, -0.02, 0.0], [0.005, 0.01, 0.02])
    assert r["hit_rate"] == 0.5  # the zero outcome is not counted
    assert math.isnan(classification_metrics([], [])["log_loss"])
    assert math.isnan(regression_metrics([], [])["mse"])
