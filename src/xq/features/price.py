"""Price-structure features (FEAT-002).

All on one timeframe's bars, at each bar's availability; "sigma units" divide a log distance by
`bar_sigma` (the timeframe's EWMA volatility of one-bar log returns, span ``sigma_span``, known at
the bar):

- ``log_return``: ``log(close_t / close_{t-bars})`` (the plan's 1, 2, 4, ..., 64 bars);
- ``range_sigma``: the bar's range ``log(high / low)`` in sigma units;
- ``candle``: ``body = (close - open) / (high - low)``, ``upper_wick = (high - max(open, close)) /
  (high - low)``, ``lower_wick = (min(open, close) - low) / (high - low)``; missing for a bar
  without range;
- ``gap``: ``log(open_t / close_{t-1})`` in sigma units of the previous bar, on a bar that does
  not start right after the previous one (the daily break and rollover, a weekend, missing bars),
  0 on a contiguous bar;
- ``extreme_distance``: over the last ``window`` bars (the current included), ``to_max =
  log(close / max high)`` and ``to_min = log(close / min low)`` in sigma units, and ``position =
  (close - min low) / (max high - min low)`` in [0, 1];
- ``session_vwap``: ``log(close / VWAP)`` in sigma units, with the VWAP of the trading day so far
  (17:00 New York roll): the typical price ``(high + low + close) / 3`` weighted by each bar's tick
  count. **Tick-volume caveat:** tick counts measure quote activity on one feed, not traded
  volume, so the feature is **gated** (``tick_volume``, C-33 (4)): it is computed and checked,
  but refused as a model input until FEAT-007 admits tick weights (cross-feed stability,
  DATA-011);
- ``ema_distance``: ``log(close / EMA_span(close))`` in sigma units.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from xq.datasets.primitives import ewm_mean, log_returns, rolling
from xq.features.base import (
    BarContext,
    Feature,
    FeatureParams,
    bar_sigma,
    column,
    contiguous,
    ewma_reach,
    frame,
    log_ratio,
    register_feature,
    safe_divide,
)


class Bars(FeatureParams):
    bars: int = Field(ge=1)


class Sigma(FeatureParams):
    sigma_span: int = Field(ge=2)


class Window(FeatureParams):
    window: int = Field(ge=2)
    sigma_span: int = Field(ge=2)


class Span(FeatureParams):
    span: int = Field(ge=2)
    sigma_span: int = Field(ge=2)


class NoParams(FeatureParams):
    pass


@register_feature(
    name="log_return",
    version=1,
    family="price",
    params=Bars,
    lookback=lambda p: p.bars + 1,
    warmup=lambda p: p.bars + 1,
)
def log_return(bars: pd.DataFrame, params: Bars, context: BarContext) -> pd.DataFrame:
    """Log return over the last `bars` bars."""
    return frame(bars, value=log_returns(column(bars, "close"), periods=params.bars))


@register_feature(
    name="range_sigma",
    version=1,
    family="price",
    params=Sigma,
    lookback=lambda p: ewma_reach(p.sigma_span),
    warmup=lambda p: p.sigma_span + 1,
)
def range_sigma(bars: pd.DataFrame, params: Sigma, context: BarContext) -> pd.DataFrame:
    """The bar's log range in sigma units."""
    spread = log_ratio(column(bars, "high"), column(bars, "low"))
    return frame(bars, value=safe_divide(spread, bar_sigma(bars, params.sigma_span)))


@register_feature(
    name="candle", version=1, family="price", params=NoParams, lookback=lambda p: 1,
    warmup=lambda p: 1,
)  # fmt: skip
def candle(bars: pd.DataFrame, params: NoParams, context: BarContext) -> pd.DataFrame:
    """Body and wick ratios of the bar's range."""
    o, h = column(bars, "open"), column(bars, "high")
    low, c = column(bars, "low"), column(bars, "close")
    span = h - low
    return frame(
        bars,
        body=safe_divide(c - o, span),
        upper_wick=safe_divide(h - np.maximum(o, c), span),
        lower_wick=safe_divide(np.minimum(o, c) - low, span),
    )


@register_feature(
    name="gap",
    version=1,
    family="price",
    params=Sigma,
    lookback=lambda p: ewma_reach(p.sigma_span) + 1,
    warmup=lambda p: p.sigma_span + 2,
)
def gap(bars: pd.DataFrame, params: Sigma, context: BarContext) -> pd.DataFrame:
    """Gap from the previous close to this open after a break, in the previous bar's sigma."""
    previous_close = column(bars, "close").shift(1)
    jump = log_ratio(column(bars, "open"), previous_close)
    scaled = safe_divide(jump, bar_sigma(bars, params.sigma_span).shift(1))
    value = scaled.where(~contiguous(bars, context.timeframe), 0.0)
    return frame(bars, value=value.where(scaled.notna()))


@register_feature(
    name="extreme_distance",
    version=1,
    family="price",
    params=Window,
    lookback=lambda p: max(p.window, ewma_reach(p.sigma_span)),
    warmup=lambda p: p.window,  # the position; the sigma-unit distances need sigma-hat too
)
def extreme_distance(bars: pd.DataFrame, params: Window, context: BarContext) -> pd.DataFrame:
    """Distance to the rolling high and low in sigma units, and the position in the range."""
    close = column(bars, "close")
    top = rolling(column(bars, "high"), params.window, "max")
    bottom = rolling(column(bars, "low"), params.window, "min")
    sigma = bar_sigma(bars, params.sigma_span)
    return frame(
        bars,
        to_max=safe_divide(log_ratio(close, top), sigma),
        to_min=safe_divide(log_ratio(close, bottom), sigma),
        position=safe_divide(close - bottom, top - bottom),
    )


@register_feature(
    name="session_vwap",
    version=1,
    family="price",
    gate="tick_volume",
    params=Sigma,
    lookback=lambda p: ewma_reach(p.sigma_span),
    warmup=lambda p: p.sigma_span + 1,
)
def session_vwap(bars: pd.DataFrame, params: Sigma, context: BarContext) -> pd.DataFrame:
    """Distance to the trading day's tick-count-weighted VWAP so far, in sigma units."""
    typical = (column(bars, "high") + column(bars, "low") + column(bars, "close")) / 3
    weight = column(bars, "tick_count").clip(lower=0)
    day = pd.Series(bars["trading_day"].to_numpy(), index=typical.index)
    traded = (typical * weight).groupby(day, sort=False).cumsum()
    total = weight.groupby(day, sort=False).cumsum()
    vwap = safe_divide(traded, total)
    distance = log_ratio(column(bars, "close"), vwap)
    return frame(bars, value=safe_divide(distance, bar_sigma(bars, params.sigma_span)))


@register_feature(
    name="ema_distance",
    version=1,
    family="price",
    params=Span,
    lookback=lambda p: max(ewma_reach(p.span), ewma_reach(p.sigma_span)),
    warmup=lambda p: max(p.span, p.sigma_span + 1),
)
def ema_distance(bars: pd.DataFrame, params: Span, context: BarContext) -> pd.DataFrame:
    """Distance to the close's EMA in sigma units."""
    close = column(bars, "close")
    ema = ewm_mean(close, span=params.span, min_periods=params.span)
    return frame(bars, value=safe_divide(log_ratio(close, ema), bar_sigma(bars, params.sigma_span)))


PRICE: tuple[Feature, ...] = (
    log_return,
    range_sigma,
    candle,
    gap,
    extreme_distance,
    session_vwap,
    ema_distance,
)
