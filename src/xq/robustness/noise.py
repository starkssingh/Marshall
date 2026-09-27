"""Noise injection (ROB-005).

A strategy whose edge is real should survive a little noise in what it sees. One that lives at
the scale of the noise collapses: a bid-ask bounce, a stale quote, or a threshold tuned to the
third decimal of a feature. Noise is added to the strategy's **inputs** only. Its fills, costs and
P&L stay on the true prices, so the curve measures how much of the edge depends on the inputs
being exact.

- **Price noise** (`noisy_prices`): every price the strategy reads gets independent Gaussian noise
  with a standard deviation of ``level`` times the spread at that instant.
- **Feature noise** (`noisy_features`): every feature value gets Gaussian noise with a standard
  deviation of ``level`` times the feature's standard deviation. That standard deviation is
  **causal**: the expanding standard deviation of the values before the row, so no row's noise
  scale depends on later data. It needs ``min_periods`` earlier values, and before that the value
  is left as it is.
- **The degradation curve** (`noise_curve`): the strategy's annualized net Sharpe ratio at each
  level. Each level above zero is drawn ``n_seeds`` times with seeds derived from the base seed,
  and summarized by the median and a 90 % band over the draws. Level 0 is the undisturbed
  strategy, evaluated once.

Reported per level: the median Sharpe ratio, its retention (over the undisturbed Sharpe ratio,
NaN unless that is positive) and the band. The **breakdown level** is the first level whose
median Sharpe ratio is not positive (NaN if none).

Noise injection is a P2 task with no R2 gate: the curve is reported, not gated.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.seeds import derive_seed, make_rng
from xq.validation.sharpe import sharpe_ratio

FloatArray = npt.NDArray[np.float64]
NoiseKind = Literal["price", "feature"]
#: Maps (noise level, seed) to the strategy's per-period net returns with that noise injected.
NoisyEvaluate = Callable[[float, int], npt.ArrayLike]


def noisy_prices(
    prices: pd.Series | pd.DataFrame, spread: pd.Series | float, level: float, seed: int
) -> pd.Series | pd.DataFrame:
    """`prices` (a series, or a frame of price columns) plus N(0, (level x spread)^2) noise.

    Each column gets its own independent draw.

    Raises:
        ValueError: for a negative level or a spread that is negative or missing.
    """
    if level < 0:
        raise ValueError("the noise level must not be negative")
    width = (
        np.full(len(prices), float(spread))
        if isinstance(spread, (int, float))
        else pd.Series(spread).reindex(prices.index).to_numpy(np.float64)
    )
    if not np.isfinite(width).all() or (width < 0).any():
        raise ValueError("the spread must be known and not negative at every price")
    if level == 0:
        return prices.copy()
    rng = make_rng(seed)
    if isinstance(prices, pd.Series):
        return prices + level * width * rng.standard_normal(len(prices))
    noise = rng.standard_normal(prices.shape) * (level * width)[:, None]
    return prices + noise


def noisy_features(
    features: pd.DataFrame, level: float, seed: int, *, min_periods: int = 20
) -> pd.DataFrame:
    """`features` plus N(0, (level x causal sigma)^2) noise per column (module docstring).

    Raises:
        ValueError: for a negative level or a `min_periods` below 2.
    """
    if level < 0:
        raise ValueError("the noise level must not be negative")
    if min_periods < 2:
        raise ValueError("min_periods must be at least 2")
    if level == 0:
        return features.copy()
    sigma = features.expanding(min_periods=min_periods).std().shift(1)
    rng = make_rng(seed)
    noise = rng.standard_normal(features.shape) * level * sigma.to_numpy(np.float64)
    result: pd.DataFrame = features + np.nan_to_num(noise, nan=0.0)
    return result


@dataclass(frozen=True)
class NoiseCurve:
    """Net Sharpe ratio against the injected noise (module docstring)."""

    kind: NoiseKind
    levels: tuple[float, ...]
    #: Annualized net Sharpe ratio per level (rows) and draw (columns); level 0 has one draw,
    #: repeated.
    sharpe: FloatArray

    @property
    def base(self) -> float:
        """The undisturbed strategy's net Sharpe ratio."""
        return float(self.sharpe[0, 0])

    def median(self) -> FloatArray:
        """Median net Sharpe ratio over the draws, per level."""
        result: FloatArray = np.nanmedian(self.sharpe, axis=1)
        return result

    def retention(self) -> FloatArray:
        """Median over the undisturbed Sharpe ratio (NaN unless that is positive)."""
        if not self.base > 0:
            return np.full(len(self.levels), math.nan)
        return self.median() / self.base

    @property
    def breakdown_level(self) -> float:
        """The first level whose median Sharpe ratio is not positive (NaN if none)."""
        for level, value in zip(self.levels, self.median(), strict=True):
            if not value > 0:
                return level
        return math.nan

    def table(self) -> pd.DataFrame:
        """One row per level: median, retention and the 90 % band over the draws."""
        return pd.DataFrame(
            {
                "median_sharpe": self.median(),
                "retention": self.retention(),
                "q05": np.nanquantile(self.sharpe, 0.05, axis=1),
                "q95": np.nanquantile(self.sharpe, 0.95, axis=1),
            },
            index=pd.Index(self.levels, name=f"{self.kind}_noise_level"),
        )


def noise_curve(
    evaluate: NoisyEvaluate,
    *,
    kind: NoiseKind,
    levels: Iterable[float],
    n_seeds: int,
    seed: int,
    periods_per_year: int,
) -> NoiseCurve:
    """The degradation curve of a strategy under noise (module docstring).

    Args:
        evaluate: Maps (level, seed) to per-period net returns with that noise injected.
        kind: What the noise is added to (for the report).
        levels: Noise levels above zero (level 0 is always evaluated first).
        n_seeds: Draws per level.
        seed: Base seed; each draw's seed is derived from it, the kind, the level and the draw.
        periods_per_year: Annualization of the Sharpe ratio.

    Raises:
        ValueError: for a negative level or fewer than one draw.
    """
    grid = tuple(sorted({float(x) for x in levels} - {0.0}))
    if any(x < 0 for x in grid) or n_seeds < 1:
        raise ValueError("levels must not be negative and n_seeds must be at least 1")
    root = math.sqrt(periods_per_year)
    base = sharpe_ratio(np.asarray(evaluate(0.0, seed), dtype=np.float64)) * root
    rows = [np.full(n_seeds, base)]
    for level in grid:
        rows.append(
            np.array(
                [
                    sharpe_ratio(
                        np.asarray(
                            evaluate(level, derive_seed(seed, "noise", kind, repr(level), draw)),
                            dtype=np.float64,
                        )
                    )
                    * root
                    for draw in range(n_seeds)
                ]
            )
        )
    return NoiseCurve(kind, (0.0, *grid), np.vstack(rows))
