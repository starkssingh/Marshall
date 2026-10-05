"""Momentum features (FEAT-003).

On one timeframe's bars, at each bar's availability:

- ``roc``: rate of change ``close_t / close_{t-bars} - 1``;
- ``rsi``: Wilder's RSI over ``window`` bars, on a 0 to 100 scale. The first average gain and loss
  are the simple means of the first ``window`` close-to-close changes; later ones follow Wilder's
  recursion ``avg_t = (avg_{t-1} * (window - 1) + x_t) / window``. ``RSI = 100 - 100 / (1 + G /
  L)``: 100 when every change in the averages is a gain, 50 when the closes do not move;
- ``macd_hist``: the MACD histogram in sigma units. ``EMA_n`` is the recursive exponential
  average ``EMA_t = a x_t + (1 - a) EMA_{t-1}`` from the first close, ``a = 2 / (n + 1)``;
  ``MACD = EMA_fast - EMA_slow`` (known once `slow` closes exist), its signal line the same EMA
  of the MACD over ``signal`` values, the histogram their difference, divided by
  ``close * sigma-hat`` (`bar_sigma`);
- ``ma_slope_t``: the t-statistic of the least-squares slope of the log close on time over the
  last ``window`` bars (the trend's slope relative to its noise);
- ``sign_agreement``: the mean of the signs of the log returns over several ``horizons`` (bars),
  in [-1, 1]: 1 when every horizon is up.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from pydantic import Field, field_validator, model_validator

from xq.datasets.primitives import log_returns, simple_returns
from xq.features.base import (
    BarContext,
    Feature,
    FeatureParams,
    bar_sigma,
    column,
    ewma_reach,
    frame,
    register_feature,
    safe_divide,
)

FloatArray = npt.NDArray[np.float64]
#: A window's residual sum of squares below this share of its total counts as none.
_NOISE_FLOOR = 1e-12


class Bars(FeatureParams):
    bars: int = Field(ge=1)


class Window(FeatureParams):
    window: int = Field(ge=2)


class Slope(FeatureParams):
    window: int = Field(ge=3)


class Macd(FeatureParams):
    fast: int = Field(ge=1)
    slow: int = Field(ge=2)
    signal: int = Field(ge=1)
    sigma_span: int = Field(ge=2)

    @model_validator(mode="after")
    def _slower(self) -> Macd:
        if self.slow <= self.fast:
            raise ValueError("slow must be longer than fast")
        return self


class Horizons(FeatureParams):
    horizons: list[int] = Field(min_length=2)

    @field_validator("horizons")
    @classmethod
    def _distinct(cls, value: list[int]) -> list[int]:
        if min(value) < 1 or len(set(value)) != len(value):
            raise ValueError("horizons must be distinct positive bar counts")
        return value


def wilder_rsi(close: FloatArray, window: int) -> FloatArray:
    """Wilder's RSI of `close` (module docstring); missing for the first `window` closes."""
    out = np.full(len(close), np.nan)
    if len(close) <= window:
        return out
    change = np.diff(close)
    gain, loss = np.maximum(change, 0.0), np.maximum(-change, 0.0)
    avg_gain, avg_loss = gain[:window].mean(), loss[:window].mean()
    out[window] = _rsi(avg_gain, avg_loss)
    for k in range(window, len(change)):
        avg_gain = (avg_gain * (window - 1) + gain[k]) / window
        avg_loss = (avg_loss * (window - 1) + loss[k]) / window
        out[k + 1] = _rsi(avg_gain, avg_loss)
    return out


def _rsi(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return float(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))


def recursive_ema(values: pd.Series, span: int) -> pd.Series:
    """``EMA_t = a x_t + (1 - a) EMA_{t-1}`` from the first value, ``a = 2 / (span + 1)``,
    missing until `span` values exist."""
    ema: pd.Series = values.ewm(span=span, adjust=False, min_periods=span).mean()
    return ema


@register_feature(
    name="roc", version=1, family="momentum", params=Bars, lookback=lambda p: p.bars + 1,
    warmup=lambda p: p.bars + 1,
)  # fmt: skip
def roc(bars: pd.DataFrame, params: Bars, context: BarContext) -> pd.DataFrame:
    """Rate of change over the last `bars` bars."""
    return frame(bars, value=simple_returns(column(bars, "close"), params.bars))


@register_feature(
    name="rsi",
    version=1,
    family="momentum",
    params=Window,
    lookback=lambda p: ewma_reach(2 * p.window - 1) + p.window,
    warmup=lambda p: p.window + 1,
)
def rsi(bars: pd.DataFrame, params: Window, context: BarContext) -> pd.DataFrame:
    """Wilder's RSI (0 to 100)."""
    return frame(bars, value=wilder_rsi(column(bars, "close").to_numpy(), params.window))


@register_feature(
    name="macd_hist",
    version=1,
    family="momentum",
    params=Macd,
    lookback=lambda p: ewma_reach(p.slow) + ewma_reach(p.signal),
    warmup=lambda p: max(p.slow + p.signal - 1, p.sigma_span + 1),
)
def macd_hist(bars: pd.DataFrame, params: Macd, context: BarContext) -> pd.DataFrame:
    """The MACD histogram in sigma units of the price."""
    close = column(bars, "close")
    macd = recursive_ema(close, params.fast) - recursive_ema(close, params.slow)
    signal = recursive_ema(macd, params.signal)
    histogram = macd - signal
    return frame(bars, value=safe_divide(histogram, close * bar_sigma(bars, params.sigma_span)))


def slope_t(values: FloatArray, window: int) -> FloatArray:
    """t-statistic of the least-squares slope of `values` on 0..window-1 over each trailing
    window; missing before the first full window or for a window without residual noise."""
    out = np.full(len(values), np.nan)
    if len(values) < window:
        return out
    windows = sliding_window_view(values, window)
    x = np.arange(window, dtype=np.float64) - (window - 1) / 2
    sxx = float(x @ x)
    centred = windows - windows.mean(axis=1, keepdims=True)
    slope = centred @ x / sxx
    total = (centred**2).sum(axis=1)
    noise = total - slope**2 * sxx
    residual = noise / (window - 2)
    # rounding leaves a residual of about 1e-16 on a perfect line: that is no noise either
    noisy = noise > _NOISE_FLOOR * total
    with np.errstate(divide="ignore", invalid="ignore"):
        stat = np.where(noisy, slope / np.sqrt(residual / sxx), np.nan)
    out[window - 1 :] = stat
    return out


@register_feature(
    name="ma_slope_t", version=1, family="momentum", params=Slope, lookback=lambda p: p.window,
    warmup=lambda p: p.window,
)  # fmt: skip
def ma_slope_t(bars: pd.DataFrame, params: Slope, context: BarContext) -> pd.DataFrame:
    """t-statistic of the log close's trend over the last `window` bars."""
    log_close = np.log(column(bars, "close").to_numpy())
    return frame(bars, value=slope_t(log_close, params.window))


@register_feature(
    name="sign_agreement",
    version=1,
    family="momentum",
    params=Horizons,
    lookback=lambda p: max(p.horizons) + 1,
    warmup=lambda p: max(p.horizons) + 1,
)
def sign_agreement(bars: pd.DataFrame, params: Horizons, context: BarContext) -> pd.DataFrame:
    """Mean sign of the log returns over several horizons."""
    close = column(bars, "close")
    signs = pd.concat([np.sign(log_returns(close, h)) for h in params.horizons], axis=1)
    value = signs.mean(axis=1).where(signs.notna().all(axis=1))
    return frame(bars, value=value)


MOMENTUM: tuple[Feature, ...] = (roc, rsi, macd_hist, ma_slope_t, sign_agreement)
