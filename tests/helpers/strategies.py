"""Simulated strategies with known truth for the Sprint 9 validation and robustness methods.

Every method of VAL-003/004/006 and ROB-001/002/003/006/007 is checked against strategies whose
answer is known before the method runs (ADR 0054):

- `noise_family`: N configurations of pure noise — no skill anywhere. PBO should be about 0.5 and
  a Reality Check or SPA test should reject at about its nominal rate.
- `graded_family`: N configurations whose true mean rises with the configuration index — a
  genuine, ordered edge. The in-sample winner keeps its rank out of sample: PBO near 0.
- `overfit_returns` / `overfit_grid`: a two-parameter strategy whose returns at every parameter
  point are independent noise (a seed derived from the point): whichever point wins in sample is
  a *single-point optimum on noise*. Its neighbours are no better than noise.
- `drift_returns` / `trend_positions`: returns carrying a slowly varying drift (a persistent AR(1)
  mean) plus noise, and a trend-following rule that holds the sign of the trailing mean of the
  last `lookback` returns — a genuine edge that survives nearby lookbacks and decays smoothly when
  entries are delayed.
"""

from __future__ import annotations

import hashlib

import numpy as np
import numpy.typing as npt
import pandas as pd

FloatArray = npt.NDArray[np.float64]


def noise_family(periods: int, configs: int, *, seed: int, sigma: float = 0.01) -> FloatArray:
    """``periods x configs`` iid N(0, sigma) returns: no configuration has any edge."""
    return np.random.default_rng(seed).normal(0.0, sigma, (periods, configs))


def graded_family(
    periods: int, configs: int, *, seed: int, top_sharpe: float = 0.15, sigma: float = 0.01
) -> FloatArray:
    """Configuration k has a true per-period Sharpe of ``top_sharpe x k / (configs - 1)``."""
    means = top_sharpe * sigma * np.arange(configs) / (configs - 1)
    return means + noise_family(periods, configs, seed=seed, sigma=sigma)


def point_seed(*params: float) -> int:
    """A seed derived from a parameter point: every point gets its own, unrelated noise."""
    digest = hashlib.sha256(repr(tuple(float(p) for p in params)).encode()).hexdigest()
    return int(digest[:12], 16)


def overfit_returns(a: float, b: float, periods: int, *, sigma: float = 0.01) -> FloatArray:
    """The returns of the overfit strategy at parameter point (a, b): pure noise."""
    return np.random.default_rng(point_seed(a, b)).normal(0.0, sigma, periods)


def overfit_grid(
    a_values: list[float], b_values: list[float], periods: int
) -> tuple[FloatArray, list[tuple[float, float]]]:
    """The configuration matrix of the overfit strategy over a parameter grid, and its points."""
    points = [(a, b) for a in a_values for b in b_values]
    matrix = np.column_stack([overfit_returns(a, b, periods) for a, b in points])
    return matrix, points


def drift_returns(
    periods: int,
    *,
    seed: int,
    persistence: float = 0.995,
    drift_sigma: float = 0.00025,
    noise_sigma: float = 0.004,
) -> FloatArray:
    """Returns with a slowly varying drift: ``r_t = mu_t + e_t``, mu an AR(1) with high
    persistence. The drift is what a trend-following rule can capture."""
    rng = np.random.default_rng(seed)
    innovations = rng.normal(0.0, drift_sigma * np.sqrt(1 - persistence**2), periods)
    mu = np.empty(periods)
    level = rng.normal(0.0, drift_sigma)
    for t in range(periods):
        level = persistence * level + innovations[t]
        mu[t] = level
    return mu + rng.normal(0.0, noise_sigma, periods)


def trend_positions(returns: FloatArray, lookback: int) -> FloatArray:
    """+1/-1 by the sign of the mean of the previous `lookback` returns (0 before that): causal."""
    series = pd.Series(returns)
    signal = series.rolling(int(lookback)).mean().shift(1)
    return np.sign(signal.fillna(0.0)).to_numpy(np.float64)


def strategy_returns(
    returns: FloatArray, positions: FloatArray, *, cost: float = 0.0, delay: int = 0
) -> FloatArray:
    """Per-period P&L of holding `positions` (taken `delay` periods late), less `cost` per unit
    of position change."""
    held = np.roll(positions, delay)
    if delay:
        held[:delay] = 0.0
    turnover = np.abs(np.diff(held, prepend=0.0))
    return held * returns - cost * turnover


def trend_family(returns: FloatArray, lookbacks: list[int], *, cost: float = 0.0) -> FloatArray:
    """The configuration matrix of the trend rule over `lookbacks`."""
    return np.column_stack(
        [strategy_returns(returns, trend_positions(returns, k), cost=cost) for k in lookbacks]
    )


def sharpe(values: FloatArray) -> float:
    """Per-period Sharpe ratio."""
    std = float(np.std(values, ddof=1))
    return float(np.mean(values)) / std if std > 0 else 0.0
