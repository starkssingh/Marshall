"""The `VolForecaster` interface (VOL-006) and the platform's sigma-hat selection.

A volatility forecaster works on a **periods frame**: one row per hour or trading day from
`xq.research.volatility.realized.realized_measures` (``ret``, ``rv``, ``bv`` ...), indexed by
each period's tz-aware decision time, in time order. The plan's interface:

- ``fit(periods_train) -> Self`` — learns from the training periods only;
- ``predict_variance(periods_upto_t, horizon)`` — at every row t, the forecast made at t's
  decision time of the variance of the next `horizon` periods' summed return, i.e. of
  ``rv_{t+1} + ... + rv_{t+horizon}``; it uses rows up to t only (a test perturbs later rows);
- ``predict(periods_upto_t, horizon)`` — sigma-hat, its square root, indexed by decision time.

Forecasters never size positions (CLAUDE.md): sigma-hat scales targets, stops and costs; sizing
is the risk engine's.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self

import numpy as np
import pandas as pd

from xq.core.errors import NaiveTimestampError

#: Columns every periods frame carries (`xq.research.volatility.realized.REALIZED_COLUMNS`).
PERIOD_COLUMNS = ("period_start", "ret", "rv")


class VolForecaster(ABC):
    """Base class of every volatility forecaster (module docstring)."""

    #: The forecaster's name on the volatility board.
    name: str = "forecaster"

    def fit(self, periods: pd.DataFrame) -> Self:
        """Fit on training periods only; return self."""
        check_periods(periods)
        if periods.empty:
            raise ValueError(f"{self.name}: no training periods")
        self._fit(periods)
        return self

    def predict_variance(self, periods: pd.DataFrame, horizon: int) -> pd.Series:
        """The variance forecast of the next `horizon` periods at every row (module docstring)."""
        check_periods(periods)
        if horizon < 1:
            raise ValueError("horizon must be at least 1 period")
        values = np.asarray(self._predict(periods, horizon), dtype=np.float64)
        if values.shape != (len(periods),):
            raise ValueError(f"{self.name} returned {values.shape} forecasts for {len(periods)}")
        return pd.Series(values, index=periods.index, name=self.name)

    def predict(self, periods: pd.DataFrame, horizon: int) -> pd.Series:
        """Sigma-hat of the next `horizon` periods' return at every row."""
        variance = self.predict_variance(periods, horizon).to_numpy(np.float64)
        return pd.Series(np.sqrt(np.maximum(variance, 0.0)), index=periods.index, name=self.name)

    @abstractmethod
    def _fit(self, periods: pd.DataFrame) -> None:
        """Learn from the training periods."""

    @abstractmethod
    def _predict(self, periods: pd.DataFrame, horizon: int) -> np.ndarray:
        """One causal variance forecast per row of `periods`."""


def check_periods(periods: pd.DataFrame) -> None:
    """Raise unless `periods` is indexed by increasing tz-aware decision times with the columns.

    Raises:
        NaiveTimestampError: for a naive index.
        ValueError: for an unsorted index or missing columns.
    """
    index = periods.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise NaiveTimestampError("periods must be indexed by tz-aware decision times")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("periods must be in strictly increasing decision-time order")
    missing = [c for c in PERIOD_COLUMNS if c not in periods.columns]
    if missing:
        raise ValueError(f"periods lack columns {missing}")
