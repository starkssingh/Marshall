"""Bars for feature tests: hand-made frames and bars built by the real bar builder."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from helpers.pipeline import REPO
from xq.core.config import load_config
from xq.core.time import trading_day
from xq.core.types import Timeframe
from xq.data.bars import build_bars
from xq.features.base import BarContext, Feature

CFG = load_config("research", config_dir=REPO / "config")
SESSIONS = CFG.sessions_config()


def hand_bars(
    close: Sequence[float],
    *,
    open_: Sequence[float] | None = None,
    high: Sequence[float] | None = None,
    low: Sequence[float] | None = None,
    ticks: Sequence[float] | None = None,
    start: str = "2024-03-12 13:00",
    timeframe: Timeframe = Timeframe.M15,
    starts: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Contiguous bars (or bars at `starts`) with the given prices; open defaults to the previous
    close, high and low to the bar's extremes of open and close."""
    c = np.asarray(close, dtype=np.float64)
    o = np.asarray(open_, dtype=np.float64) if open_ is not None else np.r_[c[0], c[:-1]]
    h = np.asarray(high, dtype=np.float64) if high is not None else np.maximum(o, c)
    lo = np.asarray(low, dtype=np.float64) if low is not None else np.minimum(o, c)
    if starts is not None:
        begin = pd.DatetimeIndex(pd.to_datetime(list(starts), utc=True))
    else:
        begin = pd.date_range(start, periods=len(c), freq=timeframe.duration, tz="UTC")
    return pd.DataFrame(
        {
            "bar_start_utc": begin,
            "available_at_utc": begin + timeframe.duration,
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "tick_count": np.asarray(ticks if ticks is not None else np.full(len(c), 10)),
            "trading_day": [trading_day(t) for t in begin],
        }
    )


def tick_bars(ticks: pd.DataFrame, tf: Timeframe) -> pd.DataFrame:
    """Complete mid bars of `ticks` as the catalog returns them (tz-aware times)."""
    bars = build_bars(
        ticks, tf, exclude_flags=0, latency_ns=0, coverage_end_ns=int(ticks["ts_utc"].max()) + 1
    )
    frame = pd.DataFrame(
        {
            "bar_start_utc": pd.to_datetime(bars["bar_start_utc"], unit="ns", utc=True),
            "available_at_utc": pd.to_datetime(bars["available_at_utc"], unit="ns", utc=True),
            "open": bars["mid_open"],
            "high": bars["mid_high"],
            "low": bars["mid_low"],
            "close": bars["mid_close"],
            "tick_count": bars["tick_count"],
            "spread_mean": bars["spread_mean"],
            "spread_max": bars["spread_max"],
            "spread_close": bars["spread_close"],
            "n_flagged": bars["n_flagged"],
            "n_excluded": bars["n_excluded"],
            "trading_day": bars["trading_day"],
        }
    )
    return frame[bars["is_complete"].to_numpy()].reset_index(drop=True)


def run(
    feature: Feature, data: pd.DataFrame, timeframe: Timeframe = Timeframe.M15, **params: object
) -> pd.DataFrame:
    """`feature` with `params` on the bars `data`, its columns as the function names them."""
    return feature.compute(data, feature.validate(params), BarContext(timeframe, SESSIONS))
