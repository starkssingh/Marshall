"""Forecast evaluation metrics and per-observation losses (BASE-006).

Probability forecasts ``p`` of a binary outcome ``y`` in {0, 1}:

- ``log_loss`` = mean(-(y log p + (1 - y) log(1 - p))), with p clipped to [1e-15, 1 - 1e-15];
- ``brier`` = mean((p - y)^2);
- ``ece`` (expected calibration error) = sum over bins of (bin share) * |mean p - mean y|, with
  `n_bins` equal-width bins on [0, 1] (the last bin includes 1); `reliability_curve` gives the
  per-bin table behind it;
- ``auc``: the probability that a random positive gets a higher forecast than a random negative
  (ties count one half), the Mann-Whitney statistic divided by n1 * n0. A secondary metric: it
  ignores calibration.

Point forecasts ``f`` of a value ``y``: ``mse``, ``mae``. Variance forecasts ``h`` of a realized
variance ``s``: ``qlike`` = mean(s / h - log(s / h) - 1) (Patton 2011), zero only for a perfect
forecast and robust to noise in the realized proxy.

`loss_series` returns the per-observation losses these means are made of, aligned with the
input, for Diebold-Mariano and related comparisons (VAL-005).
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.stats import rankdata

FloatArray = npt.NDArray[np.float64]
Loss = Literal["squared", "absolute", "log", "brier", "qlike"]
_EPS = 1e-15


def loss_series(actual: pd.Series, forecast: pd.Series, kind: Loss) -> pd.Series:
    """Per-observation loss of `forecast` against `actual` (see the module docstring).

    Raises:
        ValueError: if the indexes differ, or values are out of range for the loss.
    """
    if not actual.index.equals(forecast.index):
        raise ValueError("actual and forecast must share one index")
    y = actual.to_numpy(dtype=np.float64)
    f = forecast.to_numpy(dtype=np.float64)
    if kind == "squared":
        values = (f - y) ** 2
    elif kind == "absolute":
        values = np.abs(f - y)
    elif kind in ("log", "brier"):
        _check_binary(y)
        _check_probability(f)
        if kind == "brier":
            values = (f - y) ** 2
        else:
            p = np.clip(f, _EPS, 1 - _EPS)
            values = -(y * np.log(p) + (1 - y) * np.log(1 - p))
    else:
        if np.any(f <= 0) or np.any(y < 0):
            raise ValueError(
                "QLIKE needs positive variance forecasts and non-negative realized values"
            )
        ratio = np.maximum(y, _EPS) / f
        values = ratio - np.log(ratio) - 1
    return pd.Series(values, index=actual.index, name=f"{kind}_loss")


def log_loss(y: npt.ArrayLike, p: npt.ArrayLike) -> float:
    """Mean log loss of probability forecasts."""
    return _mean_loss(y, p, "log")


def brier(y: npt.ArrayLike, p: npt.ArrayLike) -> float:
    """Mean Brier score of probability forecasts."""
    return _mean_loss(y, p, "brier")


def mse(y: npt.ArrayLike, f: npt.ArrayLike) -> float:
    """Mean squared error."""
    return _mean_loss(y, f, "squared")


def mae(y: npt.ArrayLike, f: npt.ArrayLike) -> float:
    """Mean absolute error."""
    return _mean_loss(y, f, "absolute")


def qlike(realized: npt.ArrayLike, forecast: npt.ArrayLike) -> float:
    """Mean QLIKE loss of variance forecasts against realized variance."""
    return _mean_loss(realized, forecast, "qlike")


def reliability_curve(y: npt.ArrayLike, p: npt.ArrayLike, n_bins: int = 10) -> pd.DataFrame:
    """Per bin of forecast probability: count, mean forecast and observed frequency.

    Bins are equal-width on [0, 1]; empty bins are omitted.
    """
    if n_bins < 1:
        raise ValueError("n_bins must be positive")
    outcome, prob = np.asarray(y, np.float64), np.asarray(p, np.float64)
    _check_binary(outcome)
    _check_probability(prob)
    bins = np.minimum((prob * n_bins).astype(np.int64), n_bins - 1)
    frame = pd.DataFrame({"bin": bins, "p": prob, "y": outcome})
    table = frame.groupby("bin").agg(
        count=("y", "size"), p_mean=("p", "mean"), y_rate=("y", "mean")
    )
    table["lower"] = table.index / n_bins
    table["upper"] = (table.index + 1) / n_bins
    return table.reset_index().loc[:, ["bin", "lower", "upper", "count", "p_mean", "y_rate"]]


def ece(y: npt.ArrayLike, p: npt.ArrayLike, n_bins: int = 10) -> float:
    """Expected calibration error over equal-width bins."""
    table = reliability_curve(y, p, n_bins)
    total = table["count"].sum()
    if total == 0:
        return math.nan
    weights = table["count"] / total
    return float((weights * (table["p_mean"] - table["y_rate"]).abs()).sum())


def auc(y: npt.ArrayLike, p: npt.ArrayLike) -> float:
    """Area under the ROC curve (Mann-Whitney), NaN unless both classes occur."""
    outcome, score = np.asarray(y, np.float64), np.asarray(p, np.float64)
    _check_binary(outcome)
    positives = int(outcome.sum())
    negatives = len(outcome) - positives
    if positives == 0 or negatives == 0:
        return math.nan
    ranks = rankdata(score)  # ties get the average rank
    u = float(ranks[outcome == 1].sum()) - positives * (positives + 1) / 2
    return u / (positives * negatives)


def classification_metrics(
    y: npt.ArrayLike, p: npt.ArrayLike, n_bins: int = 10
) -> dict[str, float]:
    """Log loss, Brier, ECE, AUC and accuracy (threshold 0.5) of probability forecasts."""
    outcome, prob = np.asarray(y, np.float64), np.asarray(p, np.float64)
    if len(outcome) == 0:
        return dict.fromkeys(("log_loss", "brier", "ece", "auc", "accuracy"), math.nan)
    return {
        "log_loss": log_loss(outcome, prob),
        "brier": brier(outcome, prob),
        "ece": ece(outcome, prob, n_bins),
        "auc": auc(outcome, prob),
        "accuracy": float(np.mean((prob > 0.5) == (outcome == 1))),
    }


def regression_metrics(y: npt.ArrayLike, f: npt.ArrayLike) -> dict[str, float]:
    """MSE, MAE, sign hit rate (where neither side is zero) and mean forecast."""
    outcome, forecast = np.asarray(y, np.float64), np.asarray(f, np.float64)
    if len(outcome) == 0:
        return dict.fromkeys(("mse", "mae", "hit_rate", "mean_pred"), math.nan)
    signed = (forecast != 0) & (outcome != 0)
    hits = np.sign(forecast[signed]) == np.sign(outcome[signed])
    return {
        "mse": mse(outcome, forecast),
        "mae": mae(outcome, forecast),
        "hit_rate": float(np.mean(hits)) if signed.any() else math.nan,
        "mean_pred": float(np.mean(forecast)),
    }


def _mean_loss(y: npt.ArrayLike, f: npt.ArrayLike, kind: Loss) -> float:
    actual = pd.Series(np.asarray(y, np.float64))
    forecast = pd.Series(np.asarray(f, np.float64))
    if len(actual) != len(forecast):
        raise ValueError(f"{len(actual)} outcomes but {len(forecast)} forecasts")
    losses = loss_series(actual, forecast, kind)
    return float(losses.mean()) if len(losses) else math.nan


def _check_binary(y: FloatArray) -> None:
    if not np.isin(y, (0.0, 1.0)).all():
        raise ValueError("outcomes must be 0 or 1")


def _check_probability(p: FloatArray) -> None:
    if np.any((p < 0) | (p > 1)) or np.isnan(p).any():
        raise ValueError("probabilities must lie in [0, 1]")
