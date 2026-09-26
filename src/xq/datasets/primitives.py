"""Causal primitives for features and targets (DS-003).

Every function here is *causal*: the value at row i depends only on rows at or before i, so
computing it on a truncated series gives exactly the values computed on the full series
(truncation invariance, tested for every primitive). Rows must be in time order; a datetime index
must be tz-aware and increasing.

Deliberately absent: centered windows (banned in ``src/`` by a lint test),
backward fills, negative shifts outside target code, and any full-sample normalization. Anything
that needs "the whole sample" (a z-score against the full mean, for example) must be expressed as
an expanding or trailing statistic instead.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from xq.core.errors import NaiveTimestampError

Window = int | str
RollingStat = Literal["mean", "std", "var", "sum", "min", "max", "median"]


def rolling(
    series: pd.Series,
    window: Window,
    stat: RollingStat = "mean",
    *,
    min_periods: int | None = None,
) -> pd.Series:
    """Trailing rolling statistic over the last `window` rows (int) or time span (e.g. ``"1h"``).

    A time window covers ``(t - window, t]`` and needs a tz-aware increasing datetime index.
    `min_periods` defaults to the full row window (int) or 1 (time window).
    """
    _check_order(series)
    if isinstance(window, str):
        _require_datetime_index(series)
        periods = 1 if min_periods is None else min_periods
        roller = series.rolling(pd.Timedelta(window), min_periods=periods, closed="right")
    else:
        if window < 1:
            raise ValueError(f"window must be positive, got {window}")
        periods = window if min_periods is None else min_periods
        roller = series.rolling(window, min_periods=periods)
    result: pd.Series = getattr(roller, stat)()
    return result


def expanding(series: pd.Series, stat: RollingStat = "mean", *, min_periods: int = 1) -> pd.Series:
    """Statistic over all rows up to and including each row."""
    _check_order(series)
    result: pd.Series = getattr(series.expanding(min_periods=min_periods), stat)()
    return result


def ewm_mean(
    series: pd.Series,
    *,
    span: float | None = None,
    halflife: float | None = None,
    min_periods: int = 0,
) -> pd.Series:
    """Exponentially weighted mean with weights on past rows only (bias-adjusted)."""
    _check_order(series)
    result: pd.Series = series.ewm(
        span=span, halflife=halflife, min_periods=min_periods, adjust=True, ignore_na=True
    ).mean()
    return result


def ewm_std(
    series: pd.Series,
    *,
    span: float | None = None,
    halflife: float | None = None,
    min_periods: int = 2,
) -> pd.Series:
    """Exponentially weighted standard deviation (demeaned, bias-corrected)."""
    _check_order(series)
    result: pd.Series = series.ewm(
        span=span, halflife=halflife, min_periods=min_periods, adjust=True, ignore_na=True
    ).std()
    return result


def log_returns(prices: pd.Series, periods: int = 1) -> pd.Series:
    """``log(p_t / p_{t-periods})``; the first `periods` rows are missing."""
    _check_periods(periods)
    _check_order(prices)
    values = np.log(prices.to_numpy(dtype=np.float64))
    return _named(pd.Series(values, index=prices.index).diff(periods), prices)


def simple_returns(prices: pd.Series, periods: int = 1) -> pd.Series:
    """``p_t / p_{t-periods} - 1``; the first `periods` rows are missing."""
    _check_periods(periods)
    _check_order(prices)
    return prices.astype(np.float64).pct_change(periods, fill_method=None)


def vol_normalized(returns: pd.Series, sigma: pd.Series, *, lag: int = 1) -> pd.Series:
    """Returns divided by a volatility estimate known `lag` rows earlier.

    With the default ``lag=1`` each return is scaled by the estimate made before it, so a large
    return does not shrink itself. A zero or missing scale gives a missing value.
    """
    if lag < 0:
        raise ValueError(f"lag must not be negative, got {lag}")
    if not returns.index.equals(sigma.index):
        raise ValueError("returns and sigma must share the same index")
    _check_order(returns)
    scale = sigma.shift(lag).where(lambda s: s > 0)
    return _named(returns / scale, returns)


def realized_volatility(
    returns: pd.Series, window: Window, *, min_periods: int | None = None
) -> pd.Series:
    """Square root of the trailing sum of squared returns (not demeaned)."""
    squared = returns.astype(np.float64) ** 2
    total = rolling(squared, window, "sum", min_periods=min_periods)
    return _named(total.pow(0.5), returns)


def ewma_volatility(returns: pd.Series, *, span: float, min_periods: int = 1) -> pd.Series:
    """RiskMetrics-style volatility: square root of the EWM mean of squared returns (zero mean).

    This is the interim per-bar volatility estimate (sigma-hat) until VOL-006 selects a model.
    """
    squared = returns.astype(np.float64) ** 2
    return _named(ewm_mean(squared, span=span, min_periods=min_periods).pow(0.5), returns)


def lag(series: pd.Series, periods: int = 1) -> pd.Series:
    """The value `periods` rows earlier (a positive shift only)."""
    _check_periods(periods)
    return series.shift(periods)


def resample_causal(
    series: pd.Series, rule: str, how: Literal["last", "first", "sum", "mean", "max", "min"]
) -> pd.Series:
    """Aggregate into bins ``[start, start + rule)`` labelled by the bin *end*.

    Bins are aligned to the Unix epoch, so the grid does not depend on where the series starts.
    The label is the earliest instant the aggregate is known, so the result can be joined on
    availability. Empty bins are dropped rather than filled.
    """
    _require_datetime_index(series)
    _check_order(series)
    grouped = series.resample(rule, closed="left", label="right", origin="epoch")
    result: pd.Series = getattr(grouped, how)()
    counts = grouped.count()
    return result[counts > 0]


def _named(result: pd.Series, like: pd.Series) -> pd.Series:
    result.name = like.name
    return result


def _check_periods(periods: int) -> None:
    if periods < 1:
        raise ValueError(f"periods must be positive (no look-ahead shifts), got {periods}")


def _require_datetime_index(series: pd.Series) -> None:
    if not isinstance(series.index, pd.DatetimeIndex):
        raise TypeError("a time-based window needs a DatetimeIndex")
    if series.index.tz is None:
        raise NaiveTimestampError("naive DatetimeIndex; attach a timezone first")


def _check_order(series: pd.Series) -> None:
    if isinstance(series.index, pd.DatetimeIndex):
        if series.index.tz is None:
            raise NaiveTimestampError("naive DatetimeIndex; attach a timezone first")
        if not series.index.is_monotonic_increasing:
            raise ValueError("rows must be in increasing time order")
