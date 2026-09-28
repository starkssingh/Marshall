"""Block bootstrap of daily returns and trade-order permutation (ROB-003).

**Return intervals.** `bootstrap_returns` resamples a strategy's daily net returns (P&L over
capital, BT-002) with the stationary bootstrap (Politis and Romano 1994), whose mean block length
follows the gates' convention (Politis-White, at least ``min_block_days``), and gives percentile
intervals for:

- the annualized Sharpe ratio, ``mean / std * sqrt(P)``;
- the CAGR, ``(1 + sum r)^(P / n) - 1``: the screener sizes on constant capital, so equity is
  capital plus cumulative P&L (as `performance_metrics`), NaN for a path that loses everything;
- the maximum drawdown, the largest fall of equity from its running peak as a fraction of that
  peak, where the path starts at the capital: a loss on the first day is already a drawdown
  (BT-003's definition, `xq.backtest.metrics.path_max_drawdowns`).

**Trade order.** `permute_trades` shuffles the order of a strategy's closed trades. Their total
is unchanged, but the path is not: the distribution of the maximum drawdown and of the longest
time under water (consecutive trades below the running peak) over the orders shows how much of
the observed path was the luck of the sequence. The observed order's percentile in it says
whether the actual sequence was unusually kind (low) or clustered its losses (high).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.backtest.metrics import path_max_drawdowns, running_peak
from xq.core.seeds import make_rng
from xq.validation.sharpe import bootstrap_distribution, gate_block_length

FloatArray = npt.NDArray[np.float64]
RETURN_STATISTICS = ("sharpe", "cagr", "max_drawdown")
_CHUNK = 500


@dataclass(frozen=True)
class Interval:
    """A point estimate with its percentile interval."""

    estimate: float
    low: float
    high: float

    def covers(self, value: float) -> bool:
        """Whether `value` lies in the interval (bounds included)."""
        return self.low <= value <= self.high


@dataclass(frozen=True)
class ReturnsBootstrap:
    """Stationary-bootstrap intervals of return statistics (module docstring)."""

    sharpe: Interval
    cagr: Interval
    max_drawdown: Interval
    level: float
    mean_block: float
    n_boot: int
    #: One row per resample: ``sharpe``, ``cagr`` and ``max_drawdown``.
    draws: pd.DataFrame

    def table(self) -> pd.DataFrame:
        """One row per statistic: estimate, low and high."""
        rows = {name: getattr(self, name) for name in RETURN_STATISTICS}
        return pd.DataFrame(
            {
                "estimate": [i.estimate for i in rows.values()],
                "low": [i.low for i in rows.values()],
                "high": [i.high for i in rows.values()],
            },
            index=pd.Index(list(rows), name="statistic"),
        )


def return_statistics(paths: npt.ArrayLike, periods_per_year: int) -> FloatArray:
    """Sharpe, CAGR and maximum drawdown of each row of `paths` (module docstring), rows x 3."""
    r = np.atleast_2d(np.asarray(paths, dtype=np.float64))
    n = r.shape[1]
    std = np.std(r, axis=1, ddof=1) if n > 1 else np.full(len(r), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpe = np.where(std > 0, np.mean(r, axis=1) / std, np.nan) * math.sqrt(periods_per_year)
    equity = 1.0 + np.cumsum(r, axis=1)
    final = equity[:, -1]
    with np.errstate(invalid="ignore"):
        cagr = np.where(
            final > 0, np.power(np.maximum(final, 0.0), periods_per_year / n) - 1, np.nan
        )
    drawdown = _max_drawdown(equity)
    return np.column_stack([sharpe, cagr, drawdown])


def bootstrap_returns(
    returns: npt.ArrayLike,
    *,
    periods_per_year: int,
    n_boot: int,
    seed: int,
    level: float = 0.95,
    mean_block: float | None = None,
    min_block: int = 5,
) -> ReturnsBootstrap:
    """Percentile intervals of Sharpe, CAGR and maximum drawdown (module docstring).

    Args:
        returns: Daily net returns (P&L over capital), in time order.
        periods_per_year: Annualization (``backtest.periods_per_year``).
        n_boot: Resamples (the gates' convention: ``conventions.bootstrap.n_boot``).
        seed: Seed of the resamples.
        level: Two-sided coverage of the intervals.
        mean_block: Mean block length (default: Politis-White, at least `min_block`).
        min_block: Lower bound on the automatic block length (``min_block_days``).

    Raises:
        ValueError: for fewer than three returns, missing values or a level outside (0, 1).
    """
    r = np.asarray(returns, dtype=np.float64)
    if r.ndim != 1 or len(r) < 3:
        raise ValueError("the bootstrap needs at least three returns")
    if not np.isfinite(r).all():
        raise ValueError("returns must not contain missing or infinite values")
    if not 0 < level < 1:
        raise ValueError("level must lie in (0, 1)")
    block = (
        float(mean_block)
        if mean_block is not None
        else gate_block_length(r, "politis_white", min_block)
    )
    draws = bootstrap_distribution(
        r,
        lambda paths: return_statistics(paths, periods_per_year),
        n_boot=n_boot,
        mean_block=block,
        seed=seed,
        chunk=_CHUNK,
    )
    observed = return_statistics(r, periods_per_year)[0]
    alpha = (1 - level) / 2
    intervals = {}
    for column, name in enumerate(RETURN_STATISTICS):
        values = draws[:, column]
        values = values[np.isfinite(values)]
        low, high = np.quantile(values, [alpha, 1 - alpha]) if len(values) else (math.nan,) * 2
        intervals[name] = Interval(float(observed[column]), float(low), float(high))
    return ReturnsBootstrap(
        sharpe=intervals["sharpe"],
        cagr=intervals["cagr"],
        max_drawdown=intervals["max_drawdown"],
        level=level,
        mean_block=block,
        n_boot=n_boot,
        draws=pd.DataFrame(draws, columns=list(RETURN_STATISTICS)),
    )


@dataclass(frozen=True)
class TradePermutation:
    """Maximum drawdown and time under water over shuffled trade orders (module docstring)."""

    n_trades: int
    observed_max_drawdown: float
    observed_under_water: int
    #: Per permutation: the maximum drawdown (fraction of peak equity) ...
    max_drawdown: FloatArray
    #: ... and the longest run of consecutive trades below the running peak.
    under_water: npt.NDArray[np.int64]

    def percentile(self, statistic: str) -> float:
        """Share of permutations at or below the observed value (``max_drawdown`` or
        ``under_water``): near 1, the actual order clustered its losses."""
        values, observed = (
            (self.max_drawdown, self.observed_max_drawdown)
            if statistic == "max_drawdown"
            else (self.under_water.astype(np.float64), float(self.observed_under_water))
        )
        return float(np.mean(values <= observed + 1e-12))

    def summary(self, quantiles: tuple[float, ...] = (0.05, 0.5, 0.95)) -> pd.DataFrame:
        """Observed value, its percentile and the permutation quantiles of each statistic."""
        rows = {}
        for name, values, observed in (
            ("max_drawdown", self.max_drawdown, self.observed_max_drawdown),
            ("under_water", self.under_water.astype(np.float64), float(self.observed_under_water)),
        ):
            row = {"observed": observed, "percentile": self.percentile(name)}
            row.update({f"q{q:g}": float(np.quantile(values, q)) for q in quantiles})
            rows[name] = row
        return pd.DataFrame(rows).T


def permute_trades(
    pnl: npt.ArrayLike, *, capital: float, n_perm: int, seed: int
) -> TradePermutation:
    """Shuffle the order of closed trades' net P&L (USD) and measure each path.

    Raises:
        ValueError: without trades, with missing values or a non-positive capital.
    """
    trades = np.asarray(pnl, dtype=np.float64)
    if trades.ndim != 1 or len(trades) == 0:
        raise ValueError("the permutation needs at least one trade")
    if not np.isfinite(trades).all():
        raise ValueError("trade P&L must not contain missing or infinite values")
    if capital <= 0:
        raise ValueError("capital must be positive")
    scaled = trades / capital
    rng = make_rng(seed)
    drawdowns, under = [], []
    for start in range(0, n_perm, _CHUNK):
        size = min(_CHUNK, n_perm - start)
        order = np.argsort(rng.random((size, len(scaled))), axis=1)
        equity = 1.0 + np.cumsum(scaled[order], axis=1)
        drawdowns.append(_max_drawdown(equity))
        under.append(_longest_under_water(equity))
    observed = 1.0 + np.cumsum(scaled)[None, :]
    return TradePermutation(
        n_trades=len(trades),
        observed_max_drawdown=float(_max_drawdown(observed)[0]),
        observed_under_water=int(_longest_under_water(observed)[0]),
        max_drawdown=np.concatenate(drawdowns) if drawdowns else np.array([]),
        under_water=np.concatenate(under) if under else np.array([], dtype=np.int64),
    )


def _max_drawdown(equity: FloatArray) -> FloatArray:
    """Row-wise maximum drawdown of equity paths starting from 1 (the capital)."""
    return path_max_drawdowns(equity, 1.0)


def _longest_under_water(equity: FloatArray) -> npt.NDArray[np.int64]:
    """Row-wise longest run of consecutive points below the running peak (from 1)."""
    peak = running_peak(equity, 1.0)
    below = (equity < peak).astype(np.int64)
    count = np.cumsum(below, axis=1)
    last_reset = np.maximum.accumulate(np.where(below == 0, count, 0), axis=1)
    longest: npt.NDArray[np.int64] = (count - last_reset).max(axis=1)
    return longest
