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
input, for the forecast comparisons below (VAL-005). Each takes loss series of competing forecasts
of the same targets; lower loss is better.

- `diebold_mariano`: equal expected loss of two forecasts. ``d = loss_a - loss_b``; the variance
  of its mean uses the autocovariances up to ``horizon - 1`` (Diebold and Mariano 1995; Newey-West
  weights if that estimate is not positive); with ``harvey`` the statistic is scaled by
  ``sqrt((T + 1 - 2h + h(h - 1) / T) / T)`` and compared with Student's t with ``T - 1`` degrees
  of freedom (Harvey, Leybourne and Newbold 1997). `diebold_mariano_less` is its one-sided
  version: the p-value of "a has the lower expected loss".
- `giacomini_white`: conditional equal predictive ability (Giacomini and White 2006). With
  instruments ``h`` known before the forecast (default: a constant and the lagged loss
  difference), ``T * zbar' Omega^-1 zbar`` for ``z_t = h_(t - horizon) d_t`` is chi-squared with
  as many degrees of freedom as instruments; Omega is Newey-West with ``horizon - 1`` lags.
- `model_confidence_set`: the models that contain the best one with probability ``1 - alpha``
  (Hansen, Lunde and Nason 2011), by sequential elimination with the ``T_max`` statistic and the
  stationary bootstrap; every model gets an MCS p-value and the set is the models whose p-value is
  at least ``alpha``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.stats import chi2, norm, rankdata
from scipy.stats import t as student_t

from xq.validation.sharpe import stationary_bootstrap

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


@dataclass(frozen=True)
class ComparisonTest:
    """Result of a forecast comparison test (a positive statistic means ``a`` loses more)."""

    statistic: float
    p_value: float
    n: int
    mean_difference: float


@dataclass(frozen=True)
class ModelConfidenceSet:
    """The models in the confidence set, every model's MCS p-value and the elimination order."""

    included: list[str]
    p_values: dict[str, float]
    eliminated: list[str]


def diebold_mariano(
    loss_a: npt.ArrayLike, loss_b: npt.ArrayLike, *, horizon: int = 1, harvey: bool = True
) -> ComparisonTest:
    """Two-sided test of equal expected loss of forecasts ``a`` and ``b`` (module docstring)."""
    d = _differences(loss_a, loss_b)
    n = len(d)
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    if n < 2 * horizon + 1:
        return ComparisonTest(math.nan, math.nan, n, float(np.mean(d)) if n else math.nan)
    mean = float(np.mean(d))
    centred = d - mean
    gammas = [float(np.dot(centred[k:], centred[: n - k])) / n for k in range(horizon)]
    long_run = gammas[0] + 2 * sum(gammas[1:])
    if long_run <= 0:
        long_run = gammas[0] + 2 * sum(
            (1 - k / horizon) * g for k, g in enumerate(gammas[1:], start=1)
        )
    if long_run <= 0:
        return ComparisonTest(math.nan, math.nan, n, mean)
    statistic = mean / math.sqrt(long_run / n)
    if harvey:
        statistic *= math.sqrt((n + 1 - 2 * horizon + horizon * (horizon - 1) / n) / n)
        p_value = 2 * float(student_t.sf(abs(statistic), df=n - 1))
    else:
        p_value = 2 * float(norm.sf(abs(statistic)))
    return ComparisonTest(statistic, p_value, n, mean)


def diebold_mariano_less(
    loss_a: npt.ArrayLike, loss_b: npt.ArrayLike, *, horizon: int = 1
) -> ComparisonTest:
    """One-sided Diebold-Mariano test that forecast ``a`` has the lower expected loss.

    The Harvey-corrected statistic of `diebold_mariano`; the p-value is its lower tail under
    Student's t with ``T - 1`` degrees of freedom (a negative statistic favours ``a``).
    """
    test = diebold_mariano(loss_a, loss_b, horizon=horizon, harvey=True)
    if not math.isfinite(test.statistic):
        return test
    p_value = float(student_t.cdf(test.statistic, df=test.n - 1))
    return ComparisonTest(test.statistic, p_value, test.n, test.mean_difference)


def giacomini_white(
    loss_a: npt.ArrayLike,
    loss_b: npt.ArrayLike,
    *,
    horizon: int = 1,
    instruments: npt.ArrayLike | None = None,
) -> ComparisonTest:
    """Test conditional equal predictive ability (see the module docstring).

    Args:
        instruments: One row per observation (``T x q``), each row known when the forecast for
            that observation is made; it is lagged by `horizon`. Default: a constant and the loss
            difference itself (lagged by `horizon`).
    """
    d = _differences(loss_a, loss_b)
    n = len(d)
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    h = np.column_stack([np.ones(n), d]) if instruments is None else np.asarray(instruments, float)
    if h.ndim == 1:
        h = h[:, None]
    if len(h) != n:
        raise ValueError(f"{len(h)} instrument rows for {n} loss differences")
    z = h[: n - horizon] * d[horizon:, None]
    rows, q = z.shape
    if rows <= q:
        return ComparisonTest(math.nan, math.nan, n, float(np.mean(d)))
    zbar = z.mean(axis=0)
    omega = z.T @ z / rows
    centred = z - zbar
    for k in range(1, horizon):
        gamma = centred[k:].T @ centred[:-k] / rows
        omega = omega + (1 - k / horizon) * (gamma + gamma.T)
    try:
        statistic = float(rows * zbar @ np.linalg.solve(omega, zbar))
    except np.linalg.LinAlgError:
        return ComparisonTest(math.nan, math.nan, n, float(np.mean(d)))
    return ComparisonTest(statistic, float(chi2.sf(statistic, df=q)), n, float(np.mean(d)))


def model_confidence_set(
    losses: pd.DataFrame,
    *,
    alpha: float = 0.10,
    n_boot: int = 1000,
    mean_block: float = 5.0,
    seed: int,
) -> ModelConfidenceSet:
    """The Model Confidence Set of the columns of `losses` (one row per observation)."""
    if losses.isna().to_numpy().any():
        raise ValueError("losses must not contain missing values")
    names = [str(c) for c in losses.columns]
    values = losses.to_numpy(dtype=np.float64)
    n = len(values)
    if len(names) < 2 or n < 3:
        return ModelConfidenceSet(names, dict.fromkeys(names, 1.0), [])
    index = stationary_bootstrap(n, n_boot=n_boot, mean_block=mean_block, seed=seed)
    means = values.mean(axis=0)
    boot_means = values[index].mean(axis=1)  # n_boot x m
    alive = list(range(len(names)))
    p_values: dict[str, float] = {}
    eliminated: list[str] = []
    running = 0.0
    while len(alive) > 1:
        d = means[alive] - means[alive].mean()
        d_boot = boot_means[:, alive] - boot_means[:, alive].mean(axis=1, keepdims=True)
        spread = d_boot - d
        variance = np.mean(spread**2, axis=0)
        scale = np.sqrt(np.where(variance > 0, variance, np.nan))
        t_stat = d / scale
        observed = float(np.nanmax(t_stat))
        boot = np.nanmax(spread / scale, axis=1)
        p = float(np.mean(boot >= observed))
        running = max(running, p)
        worst = alive[int(np.nanargmax(t_stat))]
        p_values[names[worst]] = running
        eliminated.append(names[worst])
        alive.remove(worst)
    p_values[names[alive[0]]] = 1.0
    included = [name for name in names if p_values[name] >= alpha]
    return ModelConfidenceSet(included, p_values, eliminated)


def _differences(loss_a: npt.ArrayLike, loss_b: npt.ArrayLike) -> FloatArray:
    a, b = np.asarray(loss_a, dtype=np.float64), np.asarray(loss_b, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError("the loss series must have the same length")
    d = a - b
    if np.isnan(d).any():
        raise ValueError("loss series must not contain missing values")
    return d
