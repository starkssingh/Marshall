"""Market-structure features (FEAT-005).

On one timeframe's bars, at each bar's availability; "sigma units" divide a log distance by
`bar_sigma` (span ``sigma_span``):

- ``breakout``: ``up = log(close / highest high of the previous window bars)`` and ``down =
  log(close / lowest low of the previous window bars)``, in sigma units (the current bar is
  excluded from the channel, so ``up > 0`` is a breakout);
- ``efficiency_ratio``: Kaufman's ``|close_t - close_{t-window}| / sum |close_k - close_{k-1}|``
  over the window, in [0, 1];
- ``adx``: Wilder's ADX with the directional indicators ``plus_di`` and ``minus_di`` (0 to 100).
  ``+DM = up move`` when the high's rise exceeds the low's fall and is positive (else 0), ``-DM``
  the other way round; the true range, +DM and -DM are smoothed with Wilder's average (seeded
  with the mean of their first ``window`` values from the second bar on), ``DI = 100 * DM / TR``,
  ``DX = 100 |+DI - -DI| / (+DI + -DI)``, and ADX is Wilder's average of DX, seeded with the mean
  of its first ``window`` values: it exists from bar ``2 * window``;
- ``swing``: the latest **confirmed** swing high and low. Bar i is a swing high when its high is
  strictly above the highs of the ``strength`` bars on each side; that is known only when the
  ``strength`` bars after it have closed, so the swing is **confirmed at bar i + strength** and
  used from that bar's availability on (the confirmation lag). Columns: ``high`` and ``low``
  (distances of the close to them in sigma units) and ``high_age`` and ``low_age`` (bars since
  the swing bar, at least ``strength``);
- ``mean_reversion_z``: the trailing z-score of the close over ``window`` bars;
- ``compression``: the percentile rank (share of values at or below) of the current
  ``window``-bar log range among its last ``history`` values: low means compressed;
- ``prior_day``: distances of the close to the previous trading day's high and low (17:00 New York
  roll), in sigma units;
- ``prior_session``: distances of the close to the high and low of the latest **completed**
  occurrence of a configured session (bars starting inside it on the same trading day), in sigma
  units;
- ``round_distance``: ``log(close / nearest multiple of step)`` in sigma units (``step`` in price
  units: a round-number level, not a threshold).
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from pydantic import Field

from xq.core.errors import ConfigError
from xq.datasets.calendar_columns import calendar_columns
from xq.datasets.primitives import rolling
from xq.features.base import (
    BAR_START,
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
    trailing_zscore,
)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


class Channel(FeatureParams):
    window: int = Field(ge=1)
    sigma_span: int = Field(ge=2)


class Window(FeatureParams):
    window: int = Field(ge=2)


class Swing(FeatureParams):
    strength: int = Field(ge=1)
    sigma_span: int = Field(ge=2)


class Compression(FeatureParams):
    window: int = Field(ge=2)
    history: int = Field(ge=2)


class Sigma(FeatureParams):
    sigma_span: int = Field(ge=2)


class Session(FeatureParams):
    session: str
    sigma_span: int = Field(ge=2)


class Round(FeatureParams):
    step: float = Field(gt=0)
    sigma_span: int = Field(ge=2)


def _sigma_warmup(span: int) -> int:
    return span + 1


@register_feature(
    name="breakout",
    version=1,
    family="structure",
    params=Channel,
    lookback=lambda p: max(p.window + 1, ewma_reach(p.sigma_span)),
    warmup=lambda p: max(p.window + 1, _sigma_warmup(p.sigma_span)),
)
def breakout(bars: pd.DataFrame, params: Channel, context: BarContext) -> pd.DataFrame:
    """Distance beyond the previous window's high and low channel, in sigma units."""
    close = column(bars, "close")
    top = rolling(column(bars, "high"), params.window, "max").shift(1)
    bottom = rolling(column(bars, "low"), params.window, "min").shift(1)
    sigma = bar_sigma(bars, params.sigma_span)
    return frame(
        bars,
        up=safe_divide(log_ratio(close, top), sigma),
        down=safe_divide(log_ratio(close, bottom), sigma),
    )


@register_feature(
    name="efficiency_ratio", version=1, family="structure", params=Window,
    lookback=lambda p: p.window + 1, warmup=lambda p: p.window + 1,
)  # fmt: skip
def efficiency_ratio(bars: pd.DataFrame, params: Window, context: BarContext) -> pd.DataFrame:
    """Net move over the path length of the last window bars."""
    close = column(bars, "close")
    net = (close - close.shift(params.window)).abs()
    path = rolling(close.diff().abs(), params.window, "sum")
    return frame(bars, value=safe_divide(net, path))


def wilder_smooth(values: FloatArray, window: int, first: int) -> FloatArray:
    """Wilder's average of `values` from row `first` on: the mean of the first `window` values,
    then ``avg_t = avg_{t-1} + (x_t - avg_{t-1}) / window``; missing before."""
    out = np.full(len(values), np.nan)
    start = first + window - 1
    if len(values) <= start:
        return out
    out[start] = float(np.mean(values[first : start + 1]))
    for t in range(start + 1, len(values)):
        out[t] = out[t - 1] + (values[t] - out[t - 1]) / window
    return out


def wilder_adx(
    high: FloatArray, low: FloatArray, close: FloatArray, window: int
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Wilder's ADX, +DI and -DI (module docstring)."""
    n = len(close)
    up, down, tr = np.zeros(n), np.zeros(n), np.zeros(n)
    if n > 1:
        rise, fall = np.diff(high), -np.diff(low)
        up[1:] = np.where((rise > fall) & (rise > 0), rise, 0.0)
        down[1:] = np.where((fall > rise) & (fall > 0), fall, 0.0)
        previous = close[:-1]
        tr[1:] = np.maximum.reduce(
            [high[1:] - low[1:], np.abs(high[1:] - previous), np.abs(low[1:] - previous)]
        )
    atr = wilder_smooth(tr, window, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus = 100 * wilder_smooth(up, window, 1) / np.where(atr > 0, atr, np.nan)
        minus = 100 * wilder_smooth(down, window, 1) / np.where(atr > 0, atr, np.nan)
        total = plus + minus
        dx = np.where(total > 0, 100 * np.abs(plus - minus) / total, 0.0)
    dx = np.where(np.isnan(plus) | np.isnan(minus), np.nan, dx)
    adx = wilder_smooth(np.nan_to_num(dx), window, window)
    return adx, plus, minus


@register_feature(
    name="adx",
    version=1,
    family="structure",
    params=Window,
    lookback=lambda p: 2 * p.window + ewma_reach(2 * p.window - 1),
    warmup=lambda p: p.window + 1,  # the directional indicators; ADX itself from 2 * window
)
def adx(bars: pd.DataFrame, params: Window, context: BarContext) -> pd.DataFrame:
    """Wilder's ADX and directional indicators."""
    value, plus, minus = wilder_adx(
        column(bars, "high").to_numpy(),
        column(bars, "low").to_numpy(),
        column(bars, "close").to_numpy(),
        params.window,
    )
    return frame(bars, adx=value, plus_di=plus, minus_di=minus)


def confirmed_swings(
    values: FloatArray, strength: int, *, highs: bool
) -> tuple[FloatArray, IntArray]:
    """The latest swing confirmed at or before each bar: its price and the bar it formed on (-1
    and NaN before the first). A swing at bar i is confirmed at bar i + strength (module
    docstring)."""
    n = len(values)
    price = np.full(n, np.nan)
    formed = np.full(n, -1, dtype=np.int64)
    width = 2 * strength + 1
    if n < width:
        return price, formed
    windows = sliding_window_view(values if highs else -values, width)
    middle = windows[:, strength]
    others = np.delete(windows, strength, axis=1)
    is_swing = middle > others.max(axis=1)  # window ending at bar k = strength*2 .. n-1
    confirm = np.flatnonzero(is_swing) + width - 1  # the bar that completes each window
    if not len(confirm):
        return price, formed
    latest = np.searchsorted(confirm, np.arange(n), side="right") - 1
    known = latest >= 0
    formed[known] = confirm[latest[known]] - strength
    price[known] = values[formed[known]]
    return price, formed


@register_feature(
    name="swing",
    version=1,
    family="structure",
    params=Swing,
    lookback=lambda p: 2 * p.strength + 1,
    warmup=lambda p: max(2 * p.strength + 1, _sigma_warmup(p.sigma_span)),
)
def swing(bars: pd.DataFrame, params: Swing, context: BarContext) -> pd.DataFrame:
    """Distance to and age of the latest confirmed swing high and low."""
    close = column(bars, "close")
    sigma = bar_sigma(bars, params.sigma_span)
    rows = np.arange(len(bars))
    out: dict[str, pd.Series | FloatArray] = {}
    for name, highs in (("high", True), ("low", False)):
        source = column(bars, name).to_numpy()
        price, formed = confirmed_swings(source, params.strength, highs=highs)
        level = pd.Series(price, index=close.index)
        out[name] = safe_divide(log_ratio(close, level), sigma)
        age = np.where(formed >= 0, rows - formed, np.nan).astype(np.float64)
        out[f"{name}_age"] = np.where(sigma.notna().to_numpy(), age, np.nan)
    return frame(bars, **out)


@register_feature(
    name="mean_reversion_z", version=1, family="structure", params=Window,
    lookback=lambda p: p.window, warmup=lambda p: p.window,
)  # fmt: skip
def mean_reversion_z(bars: pd.DataFrame, params: Window, context: BarContext) -> pd.DataFrame:
    """Trailing z-score of the close."""
    return frame(bars, value=trailing_zscore(column(bars, "close"), params.window))


@register_feature(
    name="compression",
    version=1,
    family="structure",
    params=Compression,
    lookback=lambda p: p.window + p.history - 1,
    warmup=lambda p: p.window + p.history - 1,
)
def compression(bars: pd.DataFrame, params: Compression, context: BarContext) -> pd.DataFrame:
    """Percentile rank of the current range among its recent values."""
    top = rolling(column(bars, "high"), params.window, "max")
    bottom = rolling(column(bars, "low"), params.window, "min")
    spread = log_ratio(top, bottom).to_numpy()
    out = np.full(len(spread), np.nan)
    first = params.window + params.history - 2
    if len(spread) > first:
        windows = sliding_window_view(spread[params.window - 1 :], params.history)
        out[first:] = (windows <= windows[:, -1:]).mean(axis=1)
    return frame(bars, value=out)


def _levels_before(
    ends: IntArray, high: FloatArray, low: FloatArray, n: int
) -> tuple[FloatArray, FloatArray]:
    """For each of `n` rows, the high and low of the latest group (day or session occurrence,
    with last rows `ends`, increasing) whose last row is before it."""
    k = np.searchsorted(ends, np.arange(n), side="left") - 1
    top, bottom = np.full(n, np.nan), np.full(n, np.nan)
    known = k >= 0
    top[known], bottom[known] = high[k[known]], low[k[known]]
    return top, bottom


@register_feature(
    name="prior_day",
    version=1,
    family="structure",
    params=Sigma,
    lookback=lambda p: ewma_reach(p.sigma_span),
    warmup=lambda p: _sigma_warmup(p.sigma_span),
)
def prior_day(bars: pd.DataFrame, params: Sigma, context: BarContext) -> pd.DataFrame:
    """Distance to the previous trading day's high and low, in sigma units."""
    day = pd.Series(bars["trading_day"].to_numpy())
    group = (day != day.shift(1)).cumsum().to_numpy() - 1  # trading days in order
    frame_ = pd.DataFrame(
        {"g": group, "h": bars["high"].to_numpy(np.float64), "l": bars["low"].to_numpy(np.float64)}
    )
    stats = frame_.groupby("g", sort=True).agg(h=("h", "max"), l=("l", "min"))
    # each day's last row: its levels apply from the next row on
    ends = frame_.reset_index().groupby("g", sort=True)["index"].max().to_numpy(np.int64)
    top, bottom = _levels_before(ends, stats["h"].to_numpy(), stats["l"].to_numpy(), len(bars))
    return _distances(bars, top, bottom, params.sigma_span)


@register_feature(
    name="prior_session",
    version=1,
    family="structure",
    params=Session,
    lookback=lambda p: ewma_reach(p.sigma_span),
    warmup=lambda p: _sigma_warmup(p.sigma_span),
)
def prior_session(bars: pd.DataFrame, params: Session, context: BarContext) -> pd.DataFrame:
    """Distance to the latest completed session's high and low, in sigma units.

    Raises:
        ConfigError: for a session the calendar does not define.
    """
    names = [*context.sessions.sessions, *context.sessions.overlaps]
    if params.session not in names:
        raise ConfigError(f"no session {params.session!r} in the calendar; known: {names}")
    starts = pd.DatetimeIndex(bars[BAR_START])
    inside = calendar_columns(starts, context.sessions)[f"in_{params.session}"].to_numpy(bool)
    day = bars["trading_day"].to_numpy()
    n = len(bars)
    new_block = inside & ~np.r_[False, inside[:-1] & (day[1:] == day[:-1])]
    block = np.where(inside, np.cumsum(new_block) - 1, -1)
    high, low = bars["high"].to_numpy(np.float64), bars["low"].to_numpy(np.float64)
    if not inside.any():
        return _distances(bars, np.full(n, np.nan), np.full(n, np.nan), params.sigma_span)
    rows = pd.DataFrame({"b": block[inside], "h": high[inside], "l": low[inside]})
    rows["row"] = np.flatnonzero(inside)
    stats = rows.groupby("b", sort=True).agg(h=("h", "max"), l=("l", "min"), end=("row", "max"))
    top, bottom = _levels_before(
        stats["end"].to_numpy(np.int64), stats["h"].to_numpy(), stats["l"].to_numpy(), n
    )
    return _distances(bars, top, bottom, params.sigma_span)


def _distances(bars: pd.DataFrame, top: FloatArray, bottom: FloatArray, span: int) -> pd.DataFrame:
    close = column(bars, "close")
    sigma = bar_sigma(bars, span)
    return frame(
        bars,
        high=safe_divide(log_ratio(close, pd.Series(top, index=close.index)), sigma),
        low=safe_divide(log_ratio(close, pd.Series(bottom, index=close.index)), sigma),
    )


@register_feature(
    name="round_distance",
    version=1,
    family="structure",
    params=Round,
    lookback=lambda p: ewma_reach(p.sigma_span),
    warmup=lambda p: _sigma_warmup(p.sigma_span),
)
def round_distance(bars: pd.DataFrame, params: Round, context: BarContext) -> pd.DataFrame:
    """Distance to the nearest round-number level, in sigma units."""
    close = column(bars, "close")
    level = (close / params.step).round() * params.step
    distance = log_ratio(close, level)
    return frame(bars, value=safe_divide(distance, bar_sigma(bars, params.sigma_span)))


STRUCTURE: tuple[Feature, ...] = (
    breakout,
    efficiency_ratio,
    adx,
    swing,
    mean_reversion_z,
    compression,
    prior_day,
    prior_session,
    round_distance,
)
