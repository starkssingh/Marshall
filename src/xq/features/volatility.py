"""Volatility features (FEAT-004).

On one timeframe's bars, at each bar's availability; every value is a per-bar quantity of the
log price (never annualized, never in dollars):

- ``range_vol``: sigma per bar of one of VOL-001's trailing estimators (``close_to_close``,
  ``parkinson``, ``garman_klass``, ``rogers_satchell``, ``yang_zhang``) over ``window`` bars;
- ``atr``: Wilder's average true range over ``window`` bars relative to the close (`wilder_atr`,
  seeded with the mean of the first ``window`` true ranges);
- ``vol_ratio``: the sample standard deviation of one-bar log returns over the last ``short``
  bars divided by that over the last ``long`` bars;
- ``vol_of_vol``: the sample standard deviation, over ``window`` bars, of the log of the
  ``inner``-bar standard deviation of returns;
- ``ewma_sigma``: `bar_sigma` itself, the EWMA sigma-hat of one-bar log returns (the interim
  sigma-hat of ``fwd_returns.v1`` on the base bars);
- ``range_expansion``: the bar's log range over the mean log range of the ``window`` bars before
  it.

**Sigma-hat from VOL-006 fitted per fold** is not a dataset column: a fitted forecaster's value
depends on its training fold, so it is produced at model time inside each fold by
`xq.models.volatility.serve_sigma` (Sprint 6), never stored with the features (ADR 0066).

VOL-001's estimators refuse bars whose high and low do not bracket the open and close. Bars from
the builder always do; the leakage harness perturbs prices one column at a time, so the
estimators here read ``high = max(open, high, close)`` and ``low = min(open, low, close)``, which
leaves every real bar unchanged.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from xq.datasets.primitives import log_returns, rolling
from xq.features.base import (
    BarContext,
    Feature,
    FeatureParams,
    bar_sigma,
    column,
    ewma_reach,
    frame,
    log_ratio,
    register_feature,
    safe_divide,
)
from xq.research.volatility import estimators

Estimator = Literal["close_to_close", "parkinson", "garman_klass", "rogers_satchell", "yang_zhang"]
#: Estimators reading the previous close need one bar more.
_WITH_PREVIOUS_CLOSE = ("close_to_close", "yang_zhang")


class RangeVol(FeatureParams):
    estimator: Estimator
    window: int = Field(ge=2)


class Window(FeatureParams):
    window: int = Field(ge=1)


class Ratio(FeatureParams):
    short: int = Field(ge=2)
    long: int = Field(ge=3)

    @model_validator(mode="after")
    def _longer(self) -> Ratio:
        if self.long <= self.short:
            raise ValueError("long must be longer than short")
        return self


class VolOfVol(FeatureParams):
    window: int = Field(ge=2)
    inner: int = Field(ge=2)


class Span(FeatureParams):
    span: int = Field(ge=2)


def consistent(bars: pd.DataFrame) -> pd.DataFrame:
    """The bars with ``high`` and ``low`` widened to bracket the open and close (module
    docstring; unchanged for any real bar)."""
    out = bars.copy()
    o, c = bars["open"].to_numpy(np.float64), bars["close"].to_numpy(np.float64)
    out["high"] = np.maximum.reduce([bars["high"].to_numpy(np.float64), o, c])
    out["low"] = np.minimum.reduce([bars["low"].to_numpy(np.float64), o, c])
    return out


@register_feature(
    name="range_vol",
    version=1,
    family="volatility",
    params=RangeVol,
    lookback=lambda p: p.window + (p.estimator in _WITH_PREVIOUS_CLOSE),
    warmup=lambda p: p.window + (p.estimator in _WITH_PREVIOUS_CLOSE),
)
def range_vol(bars: pd.DataFrame, params: RangeVol, context: BarContext) -> pd.DataFrame:
    """Sigma per bar of a trailing range estimator."""
    variance = getattr(estimators, params.estimator)(consistent(bars), params.window)
    return frame(bars, value=np.sqrt(np.maximum(variance, 0.0)))


@register_feature(
    name="atr", version=1, family="volatility", params=Window,
    lookback=lambda p: ewma_reach(2 * p.window - 1) + p.window, warmup=lambda p: p.window,
)  # fmt: skip
def atr(bars: pd.DataFrame, params: Window, context: BarContext) -> pd.DataFrame:
    """Wilder's ATR relative to the close."""
    value = estimators.wilder_atr(consistent(bars), params.window)
    return frame(bars, value=value / column(bars, "close").to_numpy())


@register_feature(
    name="vol_ratio", version=1, family="volatility", params=Ratio,
    lookback=lambda p: p.long + 1, warmup=lambda p: p.long + 1,
)  # fmt: skip
def vol_ratio(bars: pd.DataFrame, params: Ratio, context: BarContext) -> pd.DataFrame:
    """Short over long standard deviation of one-bar log returns."""
    returns = log_returns(column(bars, "close"))
    short = rolling(returns, params.short, "std")
    long = rolling(returns, params.long, "std")
    return frame(bars, value=safe_divide(short, long))


@register_feature(
    name="vol_of_vol",
    version=1,
    family="volatility",
    params=VolOfVol,
    lookback=lambda p: p.inner + p.window,
    warmup=lambda p: p.inner + p.window,
)
def vol_of_vol(bars: pd.DataFrame, params: VolOfVol, context: BarContext) -> pd.DataFrame:
    """Standard deviation of the log of a rolling volatility."""
    returns = log_returns(column(bars, "close"))
    inner = rolling(returns, params.inner, "std")
    log_vol = np.log(inner.where(inner > 0))
    return frame(bars, value=rolling(log_vol, params.window, "std"))


@register_feature(
    name="ewma_sigma", version=1, family="volatility", params=Span,
    lookback=lambda p: ewma_reach(p.span), warmup=lambda p: p.span + 1,
)  # fmt: skip
def ewma_sigma(bars: pd.DataFrame, params: Span, context: BarContext) -> pd.DataFrame:
    """The EWMA sigma-hat of one-bar log returns."""
    return frame(bars, value=bar_sigma(bars, params.span))


@register_feature(
    name="range_expansion", version=1, family="volatility", params=Window,
    lookback=lambda p: p.window + 1, warmup=lambda p: p.window + 1,
)  # fmt: skip
def range_expansion(bars: pd.DataFrame, params: Window, context: BarContext) -> pd.DataFrame:
    """The bar's log range over the mean log range of the bars before it."""
    spread = log_ratio(column(bars, "high"), column(bars, "low"))
    before = rolling(spread, params.window, "mean").shift(1)
    return frame(bars, value=safe_divide(spread, before))


VOLATILITY: tuple[Feature, ...] = (
    range_vol,
    atr,
    vol_ratio,
    vol_of_vol,
    ewma_sigma,
    range_expansion,
)
