"""Simulated processes with known properties for the EDA statistics tests.

- `garch_returns`: GARCH(1,1) with standard normal innovations and unit unconditional variance —
  serially uncorrelated returns whose squares are autocorrelated (volatility clustering).
- `ar1`: a stationary AR(1) with autocorrelations phi^k.
- `hourly_returns`: a returns frame shaped like `xq.research.eda.data.bar_returns` on New York
  hours Monday to Friday, with effects injected into chosen hour-of-week buckets.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.time import trading_days

FloatArray = npt.NDArray[np.float64]


def garch_returns(n: int, *, alpha: float, beta: float, seed: int, burn: int = 1000) -> FloatArray:
    """GARCH(1,1) returns with unit unconditional variance (omega = 1 - alpha - beta)."""
    rng = np.random.default_rng(seed)
    omega = 1.0 - alpha - beta
    z = rng.standard_normal(n + burn)
    r = np.empty(n + burn)
    variance = 1.0
    for t in range(n + burn):
        r[t] = np.sqrt(variance) * z[t]
        variance = omega + alpha * r[t] ** 2 + beta * variance
    return r[burn:]


def ar1(n: int, phi: float, *, seed: int, burn: int = 1000) -> FloatArray:
    """A stationary AR(1) ``x_t = phi x_{t-1} + e_t`` with standard normal e."""
    rng = np.random.default_rng(seed)
    e = rng.standard_normal(n + burn)
    x = np.empty(n + burn)
    x[0] = e[0] / np.sqrt(1 - phi**2)
    for t in range(1, n + burn):
        x[t] = phi * x[t - 1] + e[t]
    return x[burn:]


def hourly_returns(
    weeks: int,
    *,
    seed: int,
    sd_bps: float = 10.0,
    mean_effects: Mapping[str, float] | None = None,
    vol_multipliers: Mapping[str, float] | None = None,
    first_half_only: Mapping[str, float] | None = None,
    start: str = "2021-01-04",
) -> pd.DataFrame:
    """Hourly returns on New York hours Monday 00:00 to Friday 15:00, `weeks` weeks from `start`.

    Returns are normal with `sd_bps`; `mean_effects` add a mean (bps) to hour-of-week buckets
    (``"Tue 10"``), `vol_multipliers` scale their volatility, and `first_half_only` add a mean in
    the first half of the weeks only (an effect that does not persist).
    """
    rng = np.random.default_rng(seed)
    monday = pd.Timestamp(start)  # wall-clock New York times: no DST change falls on Mon-Fri
    local = pd.DatetimeIndex(
        [
            monday + pd.Timedelta(weeks=w, days=d, hours=h)
            for w in range(weeks)
            for d in range(5)
            for h in range(24)
            if not (d == 4 and h >= 16)
        ]
    ).tz_localize("America/New_York")
    labels = [f"{('Mon', 'Tue', 'Wed', 'Thu', 'Fri')[t.weekday()]} {t.hour:02d}" for t in local]
    week = np.repeat(np.arange(weeks), len(local) // weeks)
    ret = rng.standard_normal(len(local)) * sd_bps
    for label, multiplier in (vol_multipliers or {}).items():
        mask = np.array([x == label for x in labels])
        ret[mask] *= multiplier
    for label, effect in (mean_effects or {}).items():
        ret[np.array([x == label for x in labels])] += effect
    for label, effect in (first_half_only or {}).items():
        ret[np.array([x == label for x in labels]) & (week < weeks // 2)] += effect
    starts = local.tz_convert("UTC")
    return pd.DataFrame(
        {
            "bar_start": starts,
            "ret_start": starts,
            "ret_end": starts + pd.Timedelta(hours=1),
            "trading_day": [d.item() for d in trading_days(starts)],
            "price": 2000.0,
            "close": 2000.0,
            "ret": ret / 1e4,
            "tick_count": rng.poisson(300, len(local)).astype(np.int64),
            "spread_bps": np.full(len(local), 1.5),
        }
    )
