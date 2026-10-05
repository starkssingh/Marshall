"""The feature framework (FEAT-001): feature definitions, specs and causal building blocks.

A **feature** is a registered, versioned, pure function of the bars of one timeframe:

    compute(bars, params, context) -> DataFrame

`bars` are complete bars in time order with ``bar_start_utc``, ``available_at_utc``,
``trading_day``, ``open``, ``high``, ``low``, ``close``, ``tick_count`` (and the other bar columns
the dataset builder loads); `params` is the feature's validated parameter model; `context` gives
the bars' timeframe and the calendar configuration. The result has one row per bar, indexed by
the bar's ``available_at`` (when its value is known), and one column (or several for a feature
with sub-columns, such as a one-hot encoding). Every value uses only that bar and earlier bars,
and the calendar, which is known in advance.

`register_feature` turns such a function into a `Feature`: its name (never a target prefix), code
version, family, parameter model, and how many bars it reads (``lookback``) and needs before its
first value (``warmup``), both functions of the parameters. Registration builds an immutable
value; the registry (`xq.features.registry`) is a static mapping of every family's features, so
there is no module-level mutable state.

A `FeatureSpec` is one configured use of a feature (``config/features.yaml``): the plan's
``FeatureSpec(name, version, family, timeframe, params, lookback, warmup, inputs)`` plus its
output column. Every spec of every configured feature set goes through the leakage harness
automatically (``tests/leakage/test_all_features.py``), and a registered feature that no
configured set uses fails that suite, so no feature reaches a dataset unchecked.

**Normalization** (plan, Phase 8): no global scalers. A feature is scaled by trailing statistics
(`bar_sigma`, `trailing_zscore`) or, at model time, by a `TrainingFoldScaler` fitted on one
training fold and applied to the rest. Distances "in sigma units" divide by `bar_sigma`, the
EWMA volatility of the timeframe's own one-bar log returns known at the bar.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import BaseModel, ConfigDict, ValidationError

from xq.core.config import FeatureInstanceConfig, SessionsConfig
from xq.core.errors import ConfigError
from xq.core.types import Timeframe
from xq.datasets.primitives import ewma_volatility, log_returns, rolling

AVAILABLE_AT = "available_at_utc"
BAR_START = "bar_start_utc"
#: Feature families of the plan (Phase 8); FEAT-008 reuses them on other timeframes.
FAMILIES = ("price", "momentum", "volatility", "structure", "time")
#: Prefixes reserved for targets (TGT-001's schema guard refuses them in a feature matrix).
RESERVED_PREFIXES = ("tgt_", "fwd_")
FloatArray = npt.NDArray[np.float64]


class FeatureParams(BaseModel):
    """Base of every feature's parameter model: frozen, no unknown keys, no defaults in code for
    research parameters (they live in ``config/features.yaml``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True)
class BarContext:
    """What a feature knows besides its bars: their timeframe and the calendar."""

    timeframe: Timeframe
    sessions: SessionsConfig


FeatureFn = Callable[[pd.DataFrame, Any, BarContext], pd.DataFrame]
BarsFn = Callable[[Any], int]


@dataclass(frozen=True)
class Feature:
    """A registered feature (module docstring)."""

    name: str
    version: int
    family: str
    params: type[FeatureParams]
    compute: FeatureFn
    lookback: BarsFn
    warmup: BarsFn
    description: str

    def validate(self, params: Mapping[str, Any]) -> FeatureParams:
        """The validated parameters.

        Raises:
            ConfigError: naming the feature, for missing, unknown or invalid parameters.
        """
        try:
            return self.params.model_validate(dict(params))
        except ValidationError as exc:
            raise ConfigError(f"invalid parameters for feature {self.name!r}:\n{exc}") from exc

    def spec(self, instance: FeatureInstanceConfig) -> FeatureSpec:
        """The spec of one configured use of this feature."""
        params = self.validate(instance.params)
        dumped = params.model_dump(mode="json")
        column = instance.column or default_column(self.name, dumped)
        timeframe = instance.timeframe
        return FeatureSpec(
            name=self.name,
            version=self.version,
            family=self.family,
            timeframe=timeframe,
            params=dumped,
            lookback=int(self.lookback(params)),
            warmup=int(self.warmup(params)),
            inputs=(timeframe.value if timeframe is not None else "base",),
            column=column,
        )

    def on_bars(self, spec: FeatureSpec, bars: pd.DataFrame, context: BarContext) -> pd.DataFrame:
        """This feature on `bars`, its columns named after the spec (module docstring).

        Raises:
            ValueError: if the function's output is not one row per bar, indexed by availability.
        """
        out = self.compute(bars, self.validate(spec.params), context)
        index = availability_index(bars)
        if len(out) != len(bars) or not out.index.equals(index):
            raise ValueError(f"feature {self.name!r} must return one row per bar, by availability")
        if out.shape[1] == 1:
            return out.set_axis([spec.column], axis=1)
        return out.set_axis([f"{spec.column}_{c}" for c in out.columns], axis=1)


@dataclass(frozen=True)
class FeatureSpec:
    """One configured use of a registered feature (module docstring).

    ``lookback`` is the bars a value depends on (for exponential weights, the bars carrying 99 %
    of the weight); ``warmup`` the bars before the first value of any of its columns (no value
    exists earlier; tested for every configured spec). ``timeframe`` is None for the dataset's
    base timeframe; ``inputs`` names the input frame the
    feature reads (``base`` or a context timeframe); ``column`` is its output column (a
    multi-column feature adds ``_<sub>`` suffixes; multi-timeframe columns get the prefix
    ``mtf_<timeframe>_``).
    """

    name: str
    version: int
    family: str
    timeframe: Timeframe | None
    params: Mapping[str, Any]
    lookback: int
    warmup: int
    inputs: tuple[str, ...]
    column: str


def register_feature(
    *,
    name: str,
    version: int,
    family: str,
    params: type[FeatureParams],
    lookback: BarsFn,
    warmup: BarsFn,
) -> Callable[[FeatureFn], Feature]:
    """Decorator turning a compute function into a `Feature` (an immutable value; the registry
    collects features explicitly).

    Raises:
        ConfigError: for an invalid name (a target prefix, not lower snake case), a version below
            1 or an unknown family.
    """
    if not name.replace("_", "").isalnum() or not name[:1].isalpha() or name != name.lower():
        raise ConfigError(f"feature name {name!r} must be lower snake case")
    if name.startswith(RESERVED_PREFIXES):
        raise ConfigError(f"feature name {name!r} uses a prefix reserved for targets")
    if version < 1:
        raise ConfigError(f"feature {name!r} needs a code version of at least 1")
    if family not in FAMILIES:
        raise ConfigError(f"feature {name!r}: unknown family {family!r} (one of {FAMILIES})")

    def wrap(fn: FeatureFn) -> Feature:
        return Feature(
            name=name,
            version=version,
            family=family,
            params=params,
            compute=fn,
            lookback=lookback,
            warmup=warmup,
            description=(fn.__doc__ or "").strip().split("\n")[0],
        )

    return wrap


def default_column(name: str, params: Mapping[str, Any]) -> str:
    """``<name>_<value>_<value>...`` from the parameter values in their model's order."""
    parts = [name]
    for value in params.values():
        if isinstance(value, list | tuple):
            parts.extend(_token(v) for v in value)
        else:
            parts.append(_token(value))
    return "_".join(parts)


def _token(value: object) -> str:
    text = str(value).lower().replace(".", "p").replace("-", "m")
    return "".join(c if c.isalnum() else "_" for c in text)


# --- building blocks ------------------------------------------------------------------------------


def availability_index(bars: pd.DataFrame) -> pd.DatetimeIndex:
    """The bars' availability times: the index of every feature computed on them."""
    return pd.DatetimeIndex(bars[AVAILABLE_AT], name="decision_time")


def column(bars: pd.DataFrame, name: str) -> pd.Series:
    """A bar column as float64, indexed by availability."""
    return pd.Series(bars[name].to_numpy(np.float64), index=availability_index(bars), name=name)


def frame(bars: pd.DataFrame, **columns: pd.Series | FloatArray) -> pd.DataFrame:
    """A feature output indexed by the bars' availability."""
    index = availability_index(bars)
    return pd.DataFrame(
        {k: np.asarray(v, dtype=np.float64) for k, v in columns.items()}, index=index
    )


def bar_sigma(bars: pd.DataFrame, span: int) -> pd.Series:
    """Sigma-hat per bar: the EWMA volatility (zero mean) of the timeframe's one-bar log returns,
    known at each bar (its own return included), defined from the `span`-th return on."""
    returns = log_returns(column(bars, "close"))
    return ewma_volatility(returns, span=span, min_periods=span)


def log_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """``log(numerator / denominator)`` element by element (missing where either is not
    positive), on the numerator's index."""
    a = numerator.to_numpy(np.float64)
    b = denominator.to_numpy(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        values = np.where((a > 0) & (b > 0), np.log(a / b), np.nan)
    return pd.Series(values, index=numerator.index)


def safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """`numerator / denominator`, missing where the denominator is zero or missing."""
    return numerator / denominator.where(denominator > 0)


def trailing_zscore(series: pd.Series, window: int) -> pd.Series:
    """``(x - trailing mean) / trailing std`` over the last `window` rows (the current included);
    missing until `window` rows exist or when the trailing std is zero."""
    mean = rolling(series, window, "mean")
    std = rolling(series, window, "std")
    return safe_divide(series - mean, std)


def contiguous(bars: pd.DataFrame, timeframe: Timeframe) -> npt.NDArray[np.bool_]:
    """Whether each bar starts exactly one bar after the previous one (False for the first bar
    and after a daily break, a weekend or missing bars)."""
    start = bars[BAR_START].to_numpy("datetime64[ns]").view(np.int64)
    out = np.zeros(len(start), dtype=bool)
    if len(start) > 1:
        out[1:] = np.diff(start) == timeframe.nanos
    return out


class TrainingFoldScaler:
    """Standardizes feature columns with means and standard deviations fitted on one training
    fold only, then applied to that fold's validation and test rows (the plan's normalization
    rule: no global scalers)."""

    def __init__(self) -> None:
        self.mean_: pd.Series | None = None
        self.std_: pd.Series | None = None

    def fit(
        self, features: pd.DataFrame, train_idx: Sequence[int] | npt.NDArray[np.int64]
    ) -> TrainingFoldScaler:
        """Fit on the rows `train_idx` (positions) only.

        Raises:
            ValueError: without training rows.
        """
        rows = features.iloc[np.asarray(train_idx, dtype=np.int64)]
        if rows.empty:
            raise ValueError("a training-fold scaler needs training rows")
        self.mean_ = rows.mean(numeric_only=True)
        self.std_ = rows.std(numeric_only=True, ddof=1)
        return self

    def transform(self, features: pd.DataFrame) -> pd.DataFrame:
        """The fitted columns standardized; a column constant in training becomes missing.

        Raises:
            ValueError: if the scaler is not fitted.
        """
        if self.mean_ is None or self.std_ is None:
            raise ValueError("fit the scaler on a training fold first")
        columns = list(self.mean_.index)
        scale = self.std_.where(self.std_ > 0)
        return (features[columns] - self.mean_) / scale
