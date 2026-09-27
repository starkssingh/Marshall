"""Realized measures and the train-only diurnal factor (VOL-002).

**Intraday returns** (`intraday_returns`): close-to-close log returns of consecutive bars of one
intraday timeframe (1m or 5m) inside one trading day. The return across the daily break (from the
last bar of a trading day to the first of the next, weekends included) is not an intraday return
and is left out of every realized measure; Yang-Zhang (VOL-001) is the estimator that carries it.
A return belongs to its later bar (``bar_start``, ``available_at``).

**Per period** (`realized_measures`: UTC hours ``1h`` or trading days ``1d``, 17:00 New York
roll), with the n intraday returns r_1..r_n of the period:

- ``rv = sum r_i^2`` (realized variance);
- ``bv = (pi / 2) n / (n - 1) sum_{i>=2} |r_i| |r_{i-1}|`` (bipower variation, robust to jumps;
  Barndorff-Nielsen and Shephard 2004);
- ``jump = max(rv - bv, 0)`` and ``jump_share = jump / rv``;
- ``ret = sum r_i`` (the period's intraday return) and ``n``.

A period's row is indexed by its **decision time**: the later of the period's end and the
availability of its last bar, so nothing about the period is used before the period is over.

**Diurnal factor** (`DiurnalFactor.fit`): the intraday volatility pattern, fitted on training rows
only. Rows are bucketed by their start's minutes since the trading day began (17:00 New York, so
DST is handled by construction) in steps of the bucket width. With ``day_standardized`` each
squared return (or RV) is first divided by its trading day's mean, so calm and busy days weigh
the same. The factor of a bucket is the mean over its training rows, normalized so that the
training rows' factors average 1; a bucket with fewer than ``min_count`` training rows keeps 1.
`fit` reads only rows **available at or before** ``train_end`` — rows after it may be passed and
are ignored, so perturbing test data cannot change the factor. A full-sample factor is a leak
(plan, Phase 6 failure conditions).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import DiurnalConfig
from xq.core.errors import NaiveTimestampError
from xq.core.time import TRADING_DAY_TZ, ensure_utc, trading_day_bounds, trading_days
from xq.core.types import Timeframe

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
#: Columns of a realized-measures frame (indexed by ``decision_time``).
REALIZED_COLUMNS = ("period_start", "trading_day", "n", "ret", "rv", "bv", "jump", "jump_share")
_BIPOWER = math.pi / 2
_ROLL_MINUTES = 17 * 60


def _utc(column: pd.Series | pd.Index) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(column)
    if index.tz is None:
        raise NaiveTimestampError("timestamps must be tz-aware")
    return index.tz_convert("UTC").as_unit("ns")


def _ns(index: pd.DatetimeIndex) -> IntArray:
    values: IntArray = index.to_numpy(dtype="datetime64[ns]").view(np.int64)
    return values


def intraday_returns(bars: pd.DataFrame) -> pd.DataFrame:
    """Log returns of consecutive bars inside one trading day (module docstring).

    Args:
        bars: Bars in time order with ``bar_start_utc``, ``available_at_utc`` (tz-aware) and
            ``close``.

    Returns:
        ``bar_start``, ``available_at``, ``trading_day`` and ``ret``, one row per kept return.
    """
    starts = _utc(bars["bar_start_utc"])
    available = _utc(bars["available_at_utc"])
    if not starts.is_monotonic_increasing:
        raise ValueError("bars must be in time order")
    close = bars["close"].to_numpy(dtype=np.float64)
    days = trading_days(starts)
    same_day = days[1:] == days[:-1]
    later = np.flatnonzero(same_day) + 1
    return pd.DataFrame(
        {
            "bar_start": starts[later],
            "available_at": available[later],
            "trading_day": days[later],
            "ret": np.log(close[later] / close[later - 1]),
        }
    )


def realized_measures(returns: pd.DataFrame, period: Timeframe) -> pd.DataFrame:
    """RV, bipower variation, jumps and the intraday return per hour or trading day.

    Args:
        returns: Intraday returns (`intraday_returns`).
        period: ``1h`` (UTC hours) or ``1d`` (trading days).
    """
    if period not in (Timeframe("1h"), Timeframe("1d")):
        raise ValueError("realized measures are per 1h or per 1d period")
    starts = _utc(returns["bar_start"])
    available = _utc(returns["available_at"])
    r = returns["ret"].to_numpy(dtype=np.float64)
    if period == Timeframe("1d"):
        days = np.asarray(returns["trading_day"], dtype="datetime64[D]")
        keys = days.astype("datetime64[ns]").view(np.int64)
    else:
        keys = starts.floor("h").to_numpy(dtype="datetime64[ns]").view(np.int64)
    if len(r) == 0:
        return _empty()
    change = np.r_[True, keys[1:] != keys[:-1]]
    group = np.cumsum(change) - 1
    if len(np.unique(keys)) != int(group[-1]) + 1:
        raise ValueError("returns must be in time order")
    n = np.bincount(group).astype(np.int64)
    rv = np.bincount(group, weights=r**2)
    ret = np.bincount(group, weights=r)
    pairs = group[1:] == group[:-1]
    products = np.abs(r[1:]) * np.abs(r[:-1])
    bipower_sum = np.bincount(group[1:][pairs], weights=products[pairs], minlength=len(n))
    with np.errstate(divide="ignore", invalid="ignore"):
        bv = np.where(n > 1, _BIPOWER * n / (n - 1) * bipower_sum, np.nan)
        jump = np.maximum(rv - bv, 0.0)
        share = np.where(rv > 0, jump / rv, np.nan)
    firsts = np.flatnonzero(change)
    last_available = pd.Series(_ns(available)).groupby(group).max().to_numpy()
    if period == Timeframe("1d"):
        day_list = [d.item() for d in np.asarray(returns["trading_day"], "datetime64[D]")[firsts]]
        bounds = [trading_day_bounds(d) for d in day_list]
        period_start = pd.DatetimeIndex([b[0] for b in bounds]).as_unit("ns")
        period_end = pd.DatetimeIndex([b[1] for b in bounds]).as_unit("ns")
        trading_day = np.asarray(day_list, dtype="datetime64[D]")
    else:
        period_start = pd.DatetimeIndex(pd.to_datetime(keys[firsts], utc=True)).as_unit("ns")
        period_end = period_start + pd.Timedelta(hours=1)
        trading_day = trading_days(period_start)
    decision = np.maximum(_ns(period_end), last_available)
    return pd.DataFrame(
        {
            "period_start": period_start,
            "trading_day": trading_day,
            "n": n,
            "ret": ret,
            "rv": rv,
            "bv": bv,
            "jump": jump,
            "jump_share": share,
        },
        index=pd.DatetimeIndex(pd.to_datetime(decision, utc=True), name="decision_time"),
    )


def realized_from_bars(bars: pd.DataFrame, period: Timeframe) -> pd.DataFrame:
    """`realized_measures` of the bars' intraday returns."""
    return realized_measures(intraday_returns(bars), period)


def minutes_into_trading_day(times: pd.DatetimeIndex | pd.Series) -> IntArray:
    """Minutes since the start of each instant's trading day (17:00 New York)."""
    wall = _utc(times).tz_convert(TRADING_DAY_TZ)
    minutes = wall.hour.to_numpy() * 60 + wall.minute.to_numpy()
    result: IntArray = ((minutes - _ROLL_MINUTES) % (24 * 60)).astype(np.int64)
    return result


@dataclass(frozen=True)
class DiurnalFactor:
    """The intraday variance pattern of one bucket width, fitted on training rows only."""

    bucket_minutes: int
    factors: Mapping[int, float]
    counts: Mapping[int, int]
    train_end: pd.Timestamp
    n_train: int

    @classmethod
    def fit(
        cls,
        times: pd.DatetimeIndex | pd.Series,
        available_at: pd.DatetimeIndex | pd.Series,
        values: npt.ArrayLike,
        cfg: DiurnalConfig,
        *,
        bucket_minutes: int,
        train_end: pd.Timestamp,
    ) -> DiurnalFactor:
        """Fit on the rows available at or before `train_end` (module docstring).

        Args:
            times: Each row's start (its bucket).
            available_at: When each row becomes available.
            values: Squared returns or realized variances (non-negative).

        Raises:
            ValueError: if no training row remains or a value is negative.
        """
        if bucket_minutes < 1:
            raise ValueError("buckets are at least one minute wide")
        end = ensure_utc(train_end)
        starts = _utc(times)
        x = np.asarray(values, dtype=np.float64)
        train = (_ns(_utc(available_at)) <= end.value) & np.isfinite(x)
        if not train.any():
            raise ValueError(f"no training row is available by {end}")
        if np.any(x[train] < 0):
            raise ValueError("diurnal values must be non-negative (squared returns or RV)")
        x_train = x[train]
        if cfg.day_standardized:
            days = trading_days(starts[train]).astype(np.int64)
            day_mean = pd.Series(x_train).groupby(days).transform("mean").to_numpy()
            keep = day_mean > 0
            x_train, days = x_train[keep] / day_mean[keep], days[keep]
            buckets = (minutes_into_trading_day(starts[train]) // bucket_minutes)[keep]
        else:
            buckets = minutes_into_trading_day(starts[train]) // bucket_minutes
        frame = pd.DataFrame({"bucket": buckets, "u": x_train})
        grouped = frame.groupby("bucket")["u"].agg(["mean", "size"])
        keys = [int(b) for b in grouped.index.to_numpy(dtype=np.int64)]
        means = grouped["mean"].to_numpy(dtype=np.float64)
        sizes = grouped["size"].to_numpy(dtype=np.int64)
        raw = {
            b: (float(m) if size >= cfg.min_count else 1.0)
            for b, m, size in zip(keys, means, sizes, strict=True)
        }
        weights = np.array([raw[int(b)] for b in buckets])
        scale = float(weights.mean()) if len(weights) and weights.mean() > 0 else 1.0
        factors = {b: v / scale for b, v in raw.items()}
        counts = {b: int(size) for b, size in zip(keys, sizes, strict=True)}
        return cls(bucket_minutes, factors, counts, end, int(train.sum()))

    def buckets(self, times: pd.DatetimeIndex | pd.Series) -> IntArray:
        """The bucket of every instant."""
        return minutes_into_trading_day(times) // self.bucket_minutes

    def variance_factor(self, times: pd.DatetimeIndex | pd.Series) -> FloatArray:
        """The variance factor of every instant's bucket (1 for a bucket unseen in training)."""
        return np.array([self.factors.get(int(b), 1.0) for b in self.buckets(times)])

    def next_buckets(self, bucket: int, steps: int) -> list[int]:
        """The `steps` buckets after `bucket` in the trading day's cycle of trained buckets."""
        cycle = sorted(self.factors)
        if not cycle:
            return [bucket] * steps
        position = int(np.searchsorted(cycle, bucket, side="right"))
        return [cycle[(position + k) % len(cycle)] for k in range(steps)]


def _empty() -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "period_start": pd.Series(dtype="datetime64[ns, UTC]"),
            "trading_day": pd.Series(dtype="datetime64[s]"),
            "n": pd.Series(dtype="int64"),
            **{c: pd.Series(dtype="float64") for c in ("ret", "rv", "bv", "jump", "jump_share")},
        }
    )
    frame.index = pd.DatetimeIndex([], tz="UTC", name="decision_time")
    return frame
