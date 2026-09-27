"""Performance decay over time: the R2 ``decay_trend`` gate (Phase 17).

An edge that is being arbitraged away earns less as time goes on. The test regresses the daily
net returns on time in years,

    r_t = a + b (t / P) + e_t

where P is the number of periods a year. It estimates the slope b (the change of the daily mean
return per year) with a Newey-West (Bartlett) standard error that allows for heteroskedasticity
and serial correlation, using the same default lag as VAL-001: ``floor(4 (n/100)^(2/9))``.

The one-sided p-value of a **negative** slope is ``Phi(t)``, small when the performance falls
significantly. R2 fails when it is below ``decay_trend.significance`` (0.05), so it passes when
``p >= 0.05`` (`GatesConfig.criteria`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from scipy.stats import norm

from xq.core.config import GateCheck, GatesConfig

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class DecayTrend:
    """The slope of daily net returns against time and its one-sided test (module docstring)."""

    #: Change of the daily mean net return per year.
    slope_per_year: float
    t_stat: float
    #: One-sided p-value of a negative slope.
    p_value: float
    lags: int
    n: int

    def gate_check(self, gates: GatesConfig) -> GateCheck:
        """R2 ``decay_trend``: the p-value must be at least the significance level."""
        return gates.criterion("R2", "decay_trend.significance").check(self.p_value)


def decay_trend(
    returns: npt.ArrayLike, *, periods_per_year: int, max_lag: int | None = None
) -> DecayTrend:
    """Regress daily net returns on time and test for a negative slope (module docstring).

    Raises:
        ValueError: for fewer than ten returns or missing values.
    """
    r = np.asarray(returns, dtype=np.float64)
    n = len(r)
    if n < 10:
        raise ValueError("the decay trend needs at least ten returns")
    if not np.isfinite(r).all():
        raise ValueError("returns must not contain missing values")
    years = np.arange(n) / periods_per_year
    x = np.column_stack([np.ones(n), years])
    xtx_inv = np.linalg.inv(x.T @ x)
    beta = xtx_inv @ x.T @ r
    residuals = r - x @ beta
    lags = max_lag if max_lag is not None else math.floor(4 * (n / 100) ** (2.0 / 9.0))
    scores = x * residuals[:, None]
    meat = scores.T @ scores
    for lag in range(1, min(lags, n - 1) + 1):
        gamma = scores[lag:].T @ scores[:-lag]
        meat = meat + (1 - lag / (lags + 1)) * (gamma + gamma.T)
    variance = xtx_inv @ meat @ xtx_inv
    se = math.sqrt(variance[1, 1]) if variance[1, 1] > 0 else math.nan
    t_stat = float(beta[1] / se) if se and math.isfinite(se) else math.nan
    p_value = float(norm.cdf(t_stat)) if math.isfinite(t_stat) else math.nan
    return DecayTrend(float(beta[1]), t_stat, p_value, lags, n)
