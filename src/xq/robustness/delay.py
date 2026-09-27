"""Execution-delay sensitivity (ROB-007).

The strategy's orders are sent 1, 2 and 3 bars late, and its net Sharpe ratio is traced against
the delay. A genuine edge whose information lasts longer than a bar decays **smoothly**; an edge
that exists only at the instant of the signal (a bid-ask bounce, a stale quote, information the
strategy should not have had) collapses or **flips** sign at the first delay.

- `delay_curve` evaluates any strategy at each delay (``evaluate(k)`` returns its per-period net
  returns with orders k bars late).
- `screen_delays` does so for a target-position series with the vectorized screener: the targets
  are shifted by k decision bars (`delayed_positions`), so entries *and* exits come k bars late,
  flat before the first; fills, costs and latency are unchanged.

Reported per delay: the annualized net Sharpe ratio and its *retention* (over the undelayed one).
The curve *flips* when the undelayed Sharpe is positive and a delayed one is negative. R2's
``execution_delay`` gate: the net Sharpe ratio with orders ``bars`` (1) late must exceed
``net_sharpe_min`` (0).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.backtest.costs import CostModel
from xq.backtest.vectorized import run_vectorized
from xq.core.config import GateCheck, GatesConfig
from xq.data.calendar import MarketClock
from xq.validation.sharpe import sharpe_ratio

DEFAULT_DELAYS = (0, 1, 2, 3)
FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class DelayCurve:
    """Net Sharpe ratio against the execution delay (module docstring)."""

    delays: tuple[int, ...]
    #: Annualized net Sharpe ratio at each delay.
    sharpe: FloatArray
    #: Total net return (sum of per-period net returns) at each delay.
    net_return: FloatArray

    def at(self, delay: int) -> float:
        """The net Sharpe ratio with orders `delay` bars late."""
        if delay not in self.delays:
            raise ValueError(f"delay {delay} was not evaluated; delays are {list(self.delays)}")
        return float(self.sharpe[self.delays.index(delay)])

    @property
    def retention(self) -> FloatArray:
        """Each delay's Sharpe ratio over the undelayed one (NaN unless that is positive)."""
        base = self.at(0)
        if not base > 0:
            return np.full(len(self.delays), np.nan)
        result: FloatArray = self.sharpe / base
        return result

    @property
    def flips(self) -> bool:
        """A positive undelayed Sharpe ratio turns negative at some delay."""
        return bool(self.at(0) > 0 and np.any(np.nan_to_num(self.sharpe, nan=0.0) < 0))

    def table(self) -> pd.DataFrame:
        """One row per delay: net Sharpe, retention and total net return."""
        return pd.DataFrame(
            {"sharpe": self.sharpe, "retention": self.retention, "net_return": self.net_return},
            index=pd.Index(self.delays, name="delay_bars"),
        )

    def gate_check(self, gates: GatesConfig) -> GateCheck:
        """R2 ``execution_delay``: the net Sharpe ratio at the gate's delay."""
        bars = gates.r2_validated.execution_delay.bars
        return gates.criterion("R2", "execution_delay.net_sharpe_min").check(self.at(bars))


def delay_curve(
    evaluate: Callable[[int], npt.ArrayLike],
    *,
    periods_per_year: int,
    delays: Iterable[int] = DEFAULT_DELAYS,
) -> DelayCurve:
    """Evaluate a strategy at each delay (module docstring).

    Args:
        evaluate: Maps a delay in bars to the strategy's per-period net returns.
        periods_per_year: Annualization of the Sharpe ratio.
        delays: Delays in bars; 0 (the undelayed strategy) is always included.
    """
    grid = tuple(sorted({0, *(int(d) for d in delays)}))
    if grid[0] < 0:
        raise ValueError("delays must not be negative")
    root = math.sqrt(periods_per_year)
    sharpe, total = [], []
    for delay in grid:
        returns = np.asarray(evaluate(delay), dtype=np.float64)
        sharpe.append(sharpe_ratio(returns) * root)
        total.append(float(np.sum(returns)))
    return DelayCurve(grid, np.array(sharpe), np.array(total))


def delayed_positions(positions: pd.Series, bars: int) -> pd.Series:
    """Targets `bars` decision bars late: each decision time holds the target of `bars` earlier,
    flat before the first."""
    if bars < 0:
        raise ValueError("a delay must not be negative")
    return positions.shift(bars, fill_value=0.0)


def screen_delays(
    positions: pd.Series,
    quotes: pd.DataFrame,
    costs: CostModel,
    clock: MarketClock,
    *,
    capital: float,
    periods_per_year: int,
    sigma_1m_bps: pd.Series | None = None,
    delays: Iterable[int] = DEFAULT_DELAYS,
) -> DelayCurve:
    """`delay_curve` of a target-position series screened by `run_vectorized`."""

    def evaluate(bars: int) -> FloatArray:
        result = run_vectorized(
            delayed_positions(positions, bars),
            quotes,
            costs,
            clock,
            capital=capital,
            sigma_1m_bps=sigma_1m_bps,
        )
        returns: FloatArray = result.daily["return"].to_numpy(np.float64)
        return returns

    return delay_curve(evaluate, periods_per_year=periods_per_year, delays=delays)
