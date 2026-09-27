"""Simulated processes with known properties for the EDA statistics tests.

- `garch_returns`: GARCH(1,1) with standard normal innovations and unit unconditional variance —
  serially uncorrelated returns whose squares are autocorrelated (volatility clustering).
- `ar1`: a stationary AR(1) with autocorrelations phi^k.
- `hourly_returns`: a returns frame shaped like `xq.research.eda.data.bar_returns` on New York
  hours Monday to Friday, with effects injected into chosen hour-of-week buckets.
- `minute_bars`: complete 1m mid bars on every market-open minute of the configured calendar,
  with a given log-mid path and a spread proportional to the mid.

Sprint 6 recovery tests (STAT-001 ... VOL-006) add:

- `random_walk`: a Gaussian random walk (a unit root);
- `ornstein_uhlenbeck`: an OU process sampled exactly at unit steps (an AR(1) level, mean
  reverting), whose increments have variance ratios below 1;
- `level_shift`: stationary noise around a level that shifts once;
- `garch_path`: GARCH(1,1) or GJR-GARCH(1,1) returns with any omega, and the true conditional
  variances behind them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import SessionsConfig
from xq.core.time import trading_days
from xq.data.calendar import MarketClock

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


def market_minutes(sessions: SessionsConfig, first: date, last: date) -> npt.NDArray[np.int64]:
    """UTC nanosecond starts of every market-open minute of trading days `first` to `last`."""
    clock = MarketClock.for_range(sessions, first, last)
    minute = 60 * 1_000_000_000
    return np.concatenate(
        [
            np.arange(o, c, minute, dtype=np.int64)
            for o, c in zip(clock.opens, clock.closes, strict=True)
        ]
    )


def minute_bars(
    sessions: SessionsConfig,
    first: date,
    last: date,
    *,
    log_mid: FloatArray | None = None,
    spread_bps: float = 1.0,
    price: float = 2000.0,
) -> pd.DataFrame:
    """Complete 1m mid bars on every market-open minute of trading days `first` to `last`.

    `log_mid` (one value per bar, in market-minute order) sets the closing mid
    ``price * exp(log_mid)``; the closing and mean spreads are `spread_bps` of the mid.
    """
    starts = market_minutes(sessions, first, last)
    path = np.zeros(len(starts)) if log_mid is None else np.asarray(log_mid, dtype=np.float64)
    mid = price * np.exp(path)
    stamps = pd.DatetimeIndex(pd.to_datetime(starts, unit="ns", utc=True))
    return pd.DataFrame(
        {
            "bar_start_utc": stamps,
            "available_at_utc": stamps + pd.Timedelta(minutes=1),
            "close": mid,
            "tick_count": np.full(len(starts), 60, dtype=np.int64),
            "spread_mean": mid * spread_bps / 1e4,
            "spread_close": mid * spread_bps / 1e4,
            "trading_day": [d.item() for d in trading_days(stamps)],
            "is_complete": True,
        }
    )


def random_walk(n: int, *, seed: int, sigma: float = 1.0) -> FloatArray:
    """A Gaussian random walk ``x_t = x_{t-1} + sigma e_t`` starting at 0."""
    rng = np.random.default_rng(seed)
    return np.cumsum(sigma * rng.standard_normal(n))


def ornstein_uhlenbeck(n: int, *, theta: float, sigma: float = 1.0, seed: int) -> FloatArray:
    """An OU process ``dx = -theta x dt + sigma dW`` sampled exactly at unit time steps.

    The samples are an AR(1) with ``phi = exp(-theta)`` and innovation variance
    ``sigma^2 (1 - phi^2) / (2 theta)``, started from the stationary distribution.
    """
    phi = float(np.exp(-theta))
    stationary_sd = sigma / np.sqrt(2 * theta)
    rng = np.random.default_rng(seed)
    e = rng.standard_normal(n) * stationary_sd * np.sqrt(1 - phi**2)
    x = np.empty(n)
    x[0] = rng.standard_normal() * stationary_sd
    for t in range(1, n):
        x[t] = phi * x[t - 1] + e[t]
    return x


def level_shift(n: int, *, at: int, shift: float, phi: float = 0.5, seed: int) -> FloatArray:
    """A stationary AR(1) around 0 before position `at` and around `shift` from it on."""
    return ar1(n, phi, seed=seed) + np.where(np.arange(n) >= at, shift, 0.0)


def garch_path(
    n: int,
    *,
    omega: float,
    alpha: float,
    beta: float,
    gamma: float = 0.0,
    seed: int,
    burn: int = 1000,
) -> tuple[FloatArray, FloatArray]:
    """GJR-GARCH(1,1) returns and their true conditional variances (GARCH(1,1) when gamma = 0).

    ``h_t = omega + (alpha + gamma 1[r_{t-1} < 0]) r_{t-1}^2 + beta h_{t-1}`` and
    ``r_t = sqrt(h_t) z_t`` with standard normal z; started at the unconditional variance and
    burnt in.
    """
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(n + burn)
    r = np.empty(n + burn)
    h = np.empty(n + burn)
    h[0] = omega / (1 - alpha - gamma / 2 - beta)
    for t in range(n + burn):
        if t:
            shock = r[t - 1] ** 2
            h[t] = omega + (alpha + gamma * (r[t - 1] < 0)) * shock + beta * h[t - 1]
        r[t] = np.sqrt(h[t]) * z[t]
    return r[burn:], h[burn:]


def bar_frame(
    returns: npt.ArrayLike, *, start: str = "2022-01-03", freq: str = "15min"
) -> pd.DataFrame:
    """Decision-time-indexed ``open`` / ``close`` of bars whose own log returns are `returns`.

    Each bar opens at the previous close (no gaps); the index is the bars' decision times.
    """
    r = np.asarray(returns, dtype=np.float64)
    log_close = np.cumsum(r)
    log_open = log_close - r
    index = pd.date_range(start, periods=len(r), freq=freq, tz="UTC", name="decision_time")
    return pd.DataFrame(
        {"open": 2000.0 * np.exp(log_open), "close": 2000.0 * np.exp(log_close)}, index=index
    )


def forward_target(frame: pd.DataFrame, bars: int) -> tuple[pd.Series, pd.Series]:
    """The sum of the next `bars` bar returns and its ``label_end`` (missing at the end)."""
    r = np.log(frame["close"] / frame["open"]).to_numpy(np.float64)
    n = len(r)
    cumulative = np.r_[0.0, np.cumsum(r)]
    y = np.full(n, np.nan)
    y[: n - bars] = cumulative[bars + 1 :] - cumulative[1 : n - bars + 1]
    ends = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    ends.iloc[: n - bars] = frame.index[bars:]
    return pd.Series(y, index=frame.index), ends


def completed_block_columns(frame: pd.DataFrame, bars: int, prefix: str) -> pd.DataFrame:
    """``<prefix>open`` / ``<prefix>close`` of the latest completed block of `bars` bars.

    Blocks are rows ``[k * bars, (k + 1) * bars)``; a row sees the last block ending at or before it
    (a context bar joined on availability).
    """
    n = len(frame)
    rows = np.arange(n)
    block = (rows + 1) // bars - 1  # the last block whose final row is at or before this row
    known = block >= 0
    first = np.where(known, block * bars, 0)
    last = np.where(known, block * bars + bars - 1, 0)
    opened = np.where(known, frame["open"].to_numpy()[first], np.nan)
    closed = np.where(known, frame["close"].to_numpy()[last], np.nan)
    return pd.DataFrame({f"{prefix}open": opened, f"{prefix}close": closed}, index=frame.index)


def garch_periods(
    n: int,
    *,
    omega: float,
    alpha: float,
    beta: float,
    gamma: float = 0.0,
    intraday: int = 48,
    seed: int,
    start: str = "2012-01-02 22:00",
    freq: str = "D",
) -> tuple[pd.DataFrame, FloatArray]:
    """A periods frame (`xq.research.volatility.realized`) driven by a GJR-GARCH(1,1).

    Each period's `intraday` returns are normal with variance ``h_t / intraday``; ``ret`` is their
    sum (which follows the GARCH) and ``rv`` / ``bv`` their realized variance and bipower. Rows are
    indexed by decision time every `freq` from `start`. Returns the frame and the true h.
    """
    rng = np.random.default_rng(seed)
    burn = 500
    z = rng.standard_normal((n + burn, intraday)) / np.sqrt(intraday)
    h = np.empty(n + burn)
    ret = np.empty(n + burn)
    h[0] = omega / (1 - alpha - gamma / 2 - beta)
    for t in range(n + burn):
        if t:
            shock = ret[t - 1] ** 2
            h[t] = omega + (alpha + gamma * (ret[t - 1] < 0)) * shock + beta * h[t - 1]
        ret[t] = np.sqrt(h[t]) * z[t].sum()
    intra = np.sqrt(h)[:, None] * z
    rv = (intra**2).sum(axis=1)
    bv = (
        np.pi / 2 * intraday / (intraday - 1) * np.sum(np.abs(intra[:, 1:] * intra[:, :-1]), axis=1)
    )
    index = pd.date_range(start, periods=n, freq=freq, tz="UTC", name="decision_time")
    step = index[1] - index[0] if n > 1 else pd.Timedelta(days=1)
    frame = pd.DataFrame(
        {
            "period_start": index - step,
            "n": intraday,
            "ret": ret[burn:],
            "rv": rv[burn:],
            "bv": bv[burn:],
        },
        index=index,
    )
    return frame, h[burn:]
