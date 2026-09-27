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
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.errors import ConfigError
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
