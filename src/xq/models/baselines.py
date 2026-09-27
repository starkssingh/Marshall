"""Baselines every candidate must beat on identical folds, costs and metrics (Phase 10).

**Forecast baselines (BASE-001)** are `ModelSpec`s for the walk-forward runner (WF-002). They are
benchmarks, not candidates: none has a grid, and none may be tuned.

- ``zero_return`` (regression): forecasts 0 — the random walk's forecast of a log return;
- ``random_walk`` (regression): the random walk applied to the target series itself, a
  persistence forecast: the latest realized value of the forecast quantity known at the decision
  time — the log return ``log(close / open)`` of the latest completed bar of the horizon's
  timeframe, read from the feature columns named by the ``open`` and ``close`` parameters (for
  a ``1h`` target, the ``ctx_1h_`` context bar joined on availability);
- ``historical_mean`` (regression): the mean of the fold's training targets — with expanding
  walk-forward windows, the expanding historical mean at the forecast origin (a random walk with
  drift), held for the whole test fold;
- ``climatology`` (classification): the training fold's frequency of positive targets, the
  forecast probability for every test row.

Models never size positions: turning forecasts into positions is a strategy's job (BASE-005).

**Rule strategies (BASE-002)** turn *signal bars* — the distinct context bars a dataset carries,
indexed by availability (`signal_bars`) — into a target exposure per signal bar; `positions_at`
then gives every decision time the exposure of the latest signal bar available at it (an as-of
join on availability, 0 before the first). Parameters are fixed in advance in the board
configuration and never tuned (baselines are benchmarks, not candidates). Windows are trailing
and count signal bars; every window includes the current bar unless stated:

- ``buy_and_hold``: +1 always (financing is charged by the screener);
- ``time_series_momentum``: the sign of the log return over the last ``lookback`` bars;
- ``zscore_reversion``: ``z = (close - mean) / std`` over ``lookback`` bars; short when
  ``z >= entry``, long when ``z <= -entry``; a short exits when ``z <= exit``, a long when
  ``z >= -exit`` (and may re-enter on the same bar);
- ``ma_crossover``: +1 when the ``fast`` simple moving average of closes is above the ``slow``
  one, -1 below, 0 while either is unknown;
- ``donchian_breakout``: long when the close exceeds the highest high of the ``entry`` bars
  *before* it, short when it falls below their lowest low; a long exits when the close falls
  below the lowest low of the ``exit`` bars before it or below its stop, a short symmetrically.
  The stop is fixed at entry, ``atr_stop`` average true ranges (simple mean over
  ``atr_window`` bars) from the entry close; no entry while the ATR is unknown;
- volatility-targeted versions scale any rule's exposure by
  ``min(max_exposure, annual_vol / realized)``, where ``realized`` is the standard deviation of
  the last ``lookback`` log returns of closes, annualized with ``sqrt(periods_per_year)``; the
  exposure is 0 while it is unknown.

**Random-entry null.** `random_entry` keeps a template position series' holding episodes (maximal
runs of non-zero exposure of one sign, with their exposure paths, so trade count, holding times
and the long/short mix are matched) and places them at random on the same decision grid. The
order of the episodes is a uniformly random permutation among those that fit (two neighbouring
episodes of the same side need a flat decision between them, or they would merge); if no
shuffle fits within 100 tries, the template's sequence of sides is kept and episodes are shuffled
within each side. The flat decisions left over are split into the gaps before, between and after
the episodes uniformly over all compositions. `random_entry_null` yields ``n`` such series with
seeds derived from one base seed; the board uses 1,000 (the plan's null distribution).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from xq.core.errors import ConfigError
from xq.core.seeds import derive_seed, make_rng
from xq.datasets.asof import asof_join
from xq.datasets.primitives import lag, log_returns, rolling
from xq.models.base import ModelSpec

FloatArray = npt.NDArray[np.float64]


class ConstantForecast:
    """Forecasts one number computed from the training targets (not from features)."""

    def __init__(self, statistic: Callable[[pd.Series], float]) -> None:
        self._statistic = statistic
        self.value = float("nan")

    def fit(self, x: pd.DataFrame, y: pd.Series, rng: np.random.Generator) -> None:
        """Compute the constant from the training targets `y`."""
        if len(y) == 0:
            raise ValueError("a constant forecast needs at least one training target")
        self.value = float(self._statistic(y))

    def predict(self, x: pd.DataFrame) -> FloatArray:
        """The constant for every row of `x`."""
        return np.full(len(x), self.value)


class LastBarReturn:
    """Persistence forecast: ``log(close / open)`` of the latest completed bar at each row."""

    def __init__(self, params: Mapping[str, Any]) -> None:
        try:
            self.open_column = str(params["open"])
            self.close_column = str(params["close"])
        except KeyError as exc:
            raise ConfigError(
                "random_walk needs the 'open' and 'close' feature columns of the bar whose return "
                "it repeats"
            ) from exc

    def fit(self, x: pd.DataFrame, y: pd.Series, rng: np.random.Generator) -> None:
        """Nothing to fit: the forecast is the latest realized return."""

    def predict(self, x: pd.DataFrame) -> FloatArray:
        """The latest bar's log return per row (NaN where the bar is unknown)."""
        opened = x[self.open_column].to_numpy(np.float64)
        closed = x[self.close_column].to_numpy(np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            result: FloatArray = np.log(closed / opened)
        return result


def _zero(params: Mapping[str, Any]) -> ConstantForecast:
    return ConstantForecast(lambda y: 0.0)


def _historical_mean(params: Mapping[str, Any]) -> ConstantForecast:
    return ConstantForecast(lambda y: float(y.mean()))


def _climatology(params: Mapping[str, Any]) -> ConstantForecast:
    # `y` is already 1.0 where the target is positive (`model_targets`)
    return ConstantForecast(lambda y: float(y.mean()))


def _random_walk(params: Mapping[str, Any]) -> LastBarReturn:
    return LastBarReturn(params)


FORECAST_BASELINES: Mapping[str, ModelSpec] = {
    "zero_return": ModelSpec("zero_return", 1, "regression", _zero),
    "random_walk": ModelSpec("random_walk", 1, "regression", _random_walk),
    "historical_mean": ModelSpec("historical_mean", 1, "regression", _historical_mean),
    "climatology": ModelSpec("climatology", 1, "classification", _climatology),
}


def forecast_baseline(name: str) -> ModelSpec:
    """The forecast baseline `name` (see the module docstring).

    Raises:
        ConfigError: if no such baseline exists.
    """
    try:
        return FORECAST_BASELINES[name]
    except KeyError:
        known = ", ".join(sorted(FORECAST_BASELINES))
        raise ConfigError(f"unknown forecast baseline {name!r}; available: {known}") from None


def random_walk_columns(horizon: str, base_timeframe: str) -> tuple[str, str]:
    """The ``open`` / ``close`` feature columns of the bar a ``random_walk`` forecast repeats.

    The decision bar itself when the horizon equals the base timeframe, otherwise the context bar
    of the horizon's timeframe (``ctx_<horizon>_open`` / ``ctx_<horizon>_close``).
    """
    if horizon == base_timeframe:
        return "open", "close"
    return f"ctx_{horizon}_open", f"ctx_{horizon}_close"


# --- Rule strategies (BASE-002) -------------------------------------------------------------

#: Price columns of a signal bar frame.
SIGNAL_COLUMNS = ("open", "high", "low", "close")
RuleFn = Callable[[pd.DataFrame, Mapping[str, Any]], pd.Series]


def signal_bars(features: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """The distinct context bars in a dataset's features, indexed by their availability.

    Every decision row carries the latest bar of a context timeframe (columns ``<prefix>open``
    ... ``<prefix>close``) and that bar's ``<prefix>available_at``; each bar is kept once.

    Raises:
        KeyError: if the features lack a column of that context timeframe.
    """
    availability = f"{prefix}available_at"
    columns = [availability, *(f"{prefix}{c}" for c in SIGNAL_COLUMNS)]
    missing = [c for c in columns if c not in features.columns]
    if missing:
        raise KeyError(f"features have no signal-bar columns {missing}")
    rows = features.loc[features[availability].notna(), columns]
    rows = rows.drop_duplicates(subset=availability, keep="last")
    bars = pd.DataFrame(
        {c: rows[f"{prefix}{c}"].to_numpy(np.float64) for c in SIGNAL_COLUMNS},
        index=pd.DatetimeIndex(rows[availability], name="available_at"),
    )
    return bars.sort_index()


def buy_and_hold(bars: pd.DataFrame, params: Mapping[str, Any]) -> pd.Series:
    """Long one unit of exposure on every bar."""
    return pd.Series(1.0, index=bars.index, name="exposure")


def time_series_momentum(bars: pd.DataFrame, params: Mapping[str, Any]) -> pd.Series:
    """Sign of the log return over the last ``lookback`` bars (0 until it is known)."""
    past = log_returns(bars["close"], periods=_positive(params, "lookback"))
    return _sign(past)


def ma_crossover(bars: pd.DataFrame, params: Mapping[str, Any]) -> pd.Series:
    """+1 when the fast moving average is above the slow one, -1 below (0 while unknown)."""
    fast, slow = _positive(params, "fast"), _positive(params, "slow")
    if fast >= slow:
        raise ConfigError(f"ma_crossover needs fast < slow, got {fast} and {slow}")
    close = bars["close"]
    return _sign(rolling(close, fast) - rolling(close, slow))


def zscore_reversion(bars: pd.DataFrame, params: Mapping[str, Any]) -> pd.Series:
    """Fade z-score extremes of the close against its trailing mean (module docstring)."""
    window = _positive(params, "lookback")
    entry, exit_ = float(params["entry"]), float(params["exit"])
    if not 0 <= exit_ < entry:
        raise ConfigError(f"zscore_reversion needs 0 <= exit < entry, got {exit_} and {entry}")
    close = bars["close"]
    std = rolling(close, window, "std")
    z = ((close - rolling(close, window)) / std.where(std > 0)).to_numpy(np.float64)
    state = 0.0
    out = np.zeros(len(z))
    for i, value in enumerate(z):
        if math.isnan(value):
            out[i] = state
            continue
        if (state < 0 and value <= exit_) or (state > 0 and value >= -exit_):
            state = 0.0
        if state == 0.0:
            state = -1.0 if value >= entry else 1.0 if value <= -entry else 0.0
        out[i] = state
    return pd.Series(out, index=bars.index, name="exposure")


def donchian_breakout(bars: pd.DataFrame, params: Mapping[str, Any]) -> pd.Series:
    """Channel breakout with a channel exit and an ATR stop fixed at entry (module docstring)."""
    entry, exit_ = _positive(params, "entry"), _positive(params, "exit")
    atr_window, atr_stop = _positive(params, "atr_window"), float(params["atr_stop"])
    high, low, close = bars["high"], bars["low"], bars["close"]
    upper = lag(rolling(high, entry, "max")).to_numpy(np.float64)
    lower = lag(rolling(low, entry, "min")).to_numpy(np.float64)
    exit_low = lag(rolling(low, exit_, "min")).to_numpy(np.float64)
    exit_high = lag(rolling(high, exit_, "max")).to_numpy(np.float64)
    previous = lag(close)
    true_range = pd.concat(
        [high - low, (high - previous).abs(), (low - previous).abs()], axis=1
    ).max(axis=1, skipna=False)
    atr = rolling(true_range, atr_window).to_numpy(np.float64)
    price = close.to_numpy(np.float64)
    state, stop = 0.0, math.nan
    out = np.zeros(len(price))
    for i, c in enumerate(price):
        long_exit = state > 0 and (c < exit_low[i] or c < stop)
        if long_exit or (state < 0 and (c > exit_high[i] or c > stop)):
            state = 0.0
        if state == 0.0 and not math.isnan(atr[i]):
            if c > upper[i]:
                state, stop = 1.0, c - atr_stop * atr[i]
            elif c < lower[i]:
                state, stop = -1.0, c + atr_stop * atr[i]
        out[i] = state
    return pd.Series(out, index=bars.index, name="exposure")


RULES: Mapping[str, RuleFn] = {
    "buy_and_hold": buy_and_hold,
    "time_series_momentum": time_series_momentum,
    "zscore_reversion": zscore_reversion,
    "ma_crossover": ma_crossover,
    "donchian_breakout": donchian_breakout,
}


class VolTargetConfig(BaseModel):
    """Volatility targeting of rule exposures (fixed in advance)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    annual_vol: float = Field(gt=0)
    lookback: int = Field(ge=2)
    max_exposure: float = Field(gt=0)


class RuleStrategyConfig(BaseModel):
    """One rule baseline: the rule, its fixed parameters and whether it is volatility-targeted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str
    params: dict[str, Any] = {}
    vol_target: bool = False


def vol_targeted(
    exposure: pd.Series, bars: pd.DataFrame, config: VolTargetConfig, periods_per_year: int
) -> pd.Series:
    """`exposure` scaled to the target volatility (module docstring)."""
    realized = rolling(log_returns(bars["close"]), config.lookback, "std")
    realized = realized * math.sqrt(periods_per_year)
    scale = (config.annual_vol / realized.where(realized > 0)).clip(upper=config.max_exposure)
    scale = scale.where(realized > 0, config.max_exposure).where(realized.notna(), 0.0)
    return (exposure * scale).rename("exposure")


def rule_exposure(
    bars: pd.DataFrame,
    strategy: RuleStrategyConfig,
    *,
    vol_target: VolTargetConfig | None = None,
    periods_per_year: int = 252,
) -> pd.Series:
    """Target exposure per signal bar of one rule baseline.

    Raises:
        ConfigError: for an unknown rule, bad parameters, or volatility targeting without its
            settings.
    """
    try:
        rule = RULES[strategy.rule]
    except KeyError:
        known = ", ".join(sorted(RULES))
        raise ConfigError(f"unknown rule {strategy.rule!r}; available: {known}") from None
    exposure = rule(bars, strategy.params)
    if not strategy.vol_target:
        return exposure
    if vol_target is None:
        raise ConfigError(f"rule {strategy.rule!r} is volatility-targeted but no target is set")
    return vol_targeted(exposure, bars, vol_target, periods_per_year)


def positions_at(decisions: pd.DatetimeIndex, exposure: pd.Series) -> pd.Series:
    """The exposure of the latest signal bar available at each decision time (0 before any)."""
    left = pd.DataFrame({"decision_time": decisions})
    right = pd.DataFrame(
        {"available_at": pd.DatetimeIndex(exposure.index), "exposure": exposure.to_numpy()}
    )
    joined = asof_join(left, right, on_right="available_at", columns=["exposure"])
    values = joined["exposure"].astype(np.float64).fillna(0.0).to_numpy()
    return pd.Series(values, index=decisions, name="exposure")


def holding_episodes(positions: pd.Series) -> list[tuple[int, int]]:
    """``(first row, length)`` of every maximal run of non-zero exposure of one sign."""
    sign = np.sign(positions.to_numpy(np.float64))
    episodes: list[tuple[int, int]] = []
    start: int | None = None
    for i, side in enumerate(sign):
        if start is not None and side != sign[start]:
            episodes.append((start, i - start))
            start = None
        if start is None and side != 0:
            start = i
    if start is not None:
        episodes.append((start, len(sign) - start))
    return episodes


def random_entry(positions: pd.Series, seed: int) -> pd.Series:
    """The template's holding episodes placed at random on its decision grid (module docstring).

    Raises:
        ValueError: if the positions contain missing values.
    """
    values = positions.to_numpy(np.float64)
    if np.isnan(values).any():
        raise ValueError("positions must not be missing; use 0 for flat")
    paths = [values[start : start + length] for start, length in holding_episodes(positions)]
    out = np.zeros(len(values))
    if not paths:
        return pd.Series(out, index=positions.index, name="exposure")
    sides = [float(np.sign(path[0])) for path in paths]
    free = len(values) - sum(len(path) for path in paths)
    rng = make_rng(seed)
    order: npt.NDArray[np.int64] | None = None
    for _ in range(100):
        candidate = rng.permutation(len(paths))
        if _separators(sides, candidate).sum() <= free:
            order = candidate
            break
    if order is None:  # keep the template's side sequence, shuffle within each side
        order = np.arange(len(paths))
        for side in (-1.0, 1.0):
            slots = np.array([i for i, s in enumerate(sides) if s == side], dtype=np.int64)
            order[slots] = rng.permutation(slots)
    separators = _separators(sides, order)
    gaps = _random_composition(free - int(separators.sum()), len(paths) + 1, rng)
    position = int(gaps[0])
    for j, episode in enumerate(order):
        path = paths[int(episode)]
        out[position : position + len(path)] = path
        position += len(path) + int(gaps[j + 1])
        if j < len(separators):
            position += int(separators[j])
    return pd.Series(out, index=positions.index, name="exposure")


def random_entry_null(positions: pd.Series, n: int, *, seed: int) -> Iterator[pd.Series]:
    """`n` random-entry series of `positions`, seeded ``derive_seed(seed, "random_entry", i)``."""
    for i in range(n):
        yield random_entry(positions, derive_seed(seed, "random_entry", i))


def _separators(sides: list[float], order: npt.NDArray[np.int64]) -> npt.NDArray[np.int64]:
    """1 between neighbouring episodes of the same side (they need a flat decision between)."""
    ordered = np.array([sides[int(i)] for i in order])
    result: npt.NDArray[np.int64] = (ordered[1:] == ordered[:-1]).astype(np.int64)
    return result


def _random_composition(total: int, parts: int, rng: np.random.Generator) -> npt.NDArray[np.int64]:
    """`parts` non-negative integers summing to `total`, uniform over all such compositions."""
    if total < 0:
        raise ValueError("the episodes do not fit on the decision grid")
    cuts = np.sort(rng.choice(total + parts - 1, size=parts - 1, replace=False))
    bounds = np.concatenate([[-1], cuts, [total + parts - 1]])
    result: npt.NDArray[np.int64] = (np.diff(bounds) - 1).astype(np.int64)
    return result


def _sign(values: pd.Series) -> pd.Series:
    """-1, 0 or +1 per row; 0 where the value is unknown."""
    numbers = values.to_numpy(np.float64)
    signs = np.where(np.isnan(numbers), 0.0, np.sign(numbers))
    return pd.Series(signs, index=values.index, name="exposure")


def _positive(params: Mapping[str, Any], key: str) -> int:
    try:
        value = int(params[key])
    except KeyError:
        raise ConfigError(f"rule parameter {key!r} is missing") from None
    if value < 1:
        raise ConfigError(f"rule parameter {key!r} must be at least 1, got {value}")
    return value
