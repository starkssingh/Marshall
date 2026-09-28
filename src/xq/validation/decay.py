"""Performance decay over time: the R2 ``decay_trend`` gate (Phase 17).

An edge that is being arbitraged away earns less as time goes on. The test regresses the
strategy's **performance per walk-forward test fold** on time:

    m_k = a + b t_k + e_k

where ``m_k`` is fold k's mean daily net return and ``t_k`` its midpoint in years. The slope b is
the change of the daily mean return per year. Its ordinary least-squares t-statistic is compared
with a Student t on ``K - 2`` degrees of freedom (K folds). The one-sided p-value of a
**negative** slope is small when performance falls significantly. R2 fails when it is below
``decay_trend.significance`` (0.05), so it passes when ``p >= 0.05`` (`GatesConfig.criteria`).

**Why fold means, not daily returns.** A real edge's strength varies in slow regimes. A
regression of daily returns on time with a Newey-West standard error at VAL-001's default lag
(``floor(4 (n/100)^(2/9))``, 7 lags on 20 years) understates the variance of the slope. On the
simulated genuine trend edge (ADR 0056) it rejected a stable edge at 14 % at the 5 % level, and
at 10 % even with ``sqrt(n)`` lags. Averaging within folds (batch means) absorbs that
dependence. Over 300 simulated genuine edges the fold test rejects at 6 %, and at 4 % on iid
returns. It detects an edge falling from 20 bp to -10 bp a day over 1,000 days in about 80 % of
samples (ten folds).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.stats import t as student_t

from xq.core.config import GateCheck, GatesConfig

FloatArray = npt.NDArray[np.float64]
MIN_FOLDS = 4


@dataclass(frozen=True)
class DecayTrend:
    """The slope of performance against time and its one-sided test (module docstring)."""

    #: Change of the daily mean net return per year.
    slope_per_year: float
    t_stat: float
    #: One-sided p-value of a negative slope.
    p_value: float
    #: Folds regressed.
    n_folds: int

    def gate_check(self, gates: GatesConfig) -> GateCheck:
        """R2 ``decay_trend``: the p-value must be at least the significance level."""
        return gates.criterion("R2", "decay_trend.significance").check(self.p_value)


def decay_trend(
    returns: npt.ArrayLike, folds: npt.ArrayLike, *, periods_per_year: int
) -> DecayTrend:
    """Regress fold mean returns on time and test for a negative slope (module docstring).

    Args:
        returns: Daily net returns in time order.
        folds: The walk-forward test fold of each day (contiguous runs of equal labels).
        periods_per_year: Converts day positions into years.

    Raises:
        ValueError: for missing returns, misaligned folds or fewer than four folds.
    """
    r = np.asarray(returns, dtype=np.float64)
    labels = np.asarray(folds)
    if len(labels) != len(r):
        raise ValueError("every day needs its fold")
    if not np.isfinite(r).all():
        raise ValueError("returns must not contain missing values")
    frame = pd.DataFrame({"r": r, "fold": labels, "t": np.arange(len(r)) / periods_per_year})
    by_fold = frame.groupby("fold", sort=False).agg(mean=("r", "mean"), t=("t", "mean"))
    k = len(by_fold)
    if k < MIN_FOLDS:
        raise ValueError(f"the decay trend needs at least {MIN_FOLDS} folds, got {k}")
    x = by_fold["t"].to_numpy(np.float64)
    y = by_fold["mean"].to_numpy(np.float64)
    dx = x - x.mean()
    slope = float(dx @ (y - y.mean()) / (dx @ dx))
    residuals = y - y.mean() - slope * dx
    variance = float(residuals @ residuals) / (k - 2) / float(dx @ dx)
    se = math.sqrt(variance)
    t_stat = _t_stat(slope, se)
    p_value = float(student_t.cdf(t_stat, k - 2)) if not math.isnan(t_stat) else math.nan
    return DecayTrend(slope, t_stat, p_value, k)


def _t_stat(slope: float, se: float) -> float:
    """The slope over its standard error; an exact fit is infinitely significant."""
    if se > 0:
        return slope / se
    return math.copysign(math.inf, slope) if slope else math.nan
