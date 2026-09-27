"""Volatility benchmarks with parameters fixed in advance (VOL-003).

All are `VolForecaster`s on a periods frame (hours or trading days, `xq.models.volatility`); the
forecast at row t is the variance of the next ``h`` periods' summed return:

- ``rolling_<w>``: the mean RV of the last w periods (t included), times h;
- ``ewma_<lambda>`` (RiskMetrics): ``s_{t+1} = lambda s_t + (1 - lambda) ret_t^2``, started at the
  mean squared return of the training periods, times h; lambda is fixed in advance (0.94, 0.97)
  and never tuned;
- ``har`` (Corsi 2009): for each horizon h, OLS on the training periods of
  ``rv_{t+1} + ... + rv_{t+h}`` on a constant and the mean RV over the last c periods for each
  component c (1, 5, 22 trading days on daily periods; 1, 23, 115 hours on hourly periods). Only
  training rows whose h future periods are also training rows enter the fit. Forecasts are floored
  at ``variance_floor_share`` of the mean training RV times h (a linear HAR can go negative).

`Deseasonalized` wraps any forecaster for hourly periods: it fits the diurnal factor (VOL-002) on
the training periods' RV only, divides returns by the factor's square root and RV and bipower by
the factor, fits the inner forecaster on that, and turns its forecast back into raw variance with
the factors of the next h buckets of the trading day's cycle (their timestamps are known from the
calendar, never read from future rows): ``(inner / h) * sum_k f_{t+k}``.

`benchmark_forecasters` builds the configured set by name; on hourly periods each benchmark is
deseasonalized.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import DiurnalConfig, VolatilityConfig
from xq.core.types import Timeframe
from xq.models.volatility import VolForecaster
from xq.research.volatility.realized import DiurnalFactor

FloatArray = npt.NDArray[np.float64]
Factory = Callable[[], VolForecaster]


def _trailing_mean(values: FloatArray, window: int) -> FloatArray:
    result: FloatArray = (
        pd.Series(values).rolling(window, min_periods=window).mean().to_numpy(np.float64)
    )
    return result


def _future_sum(values: FloatArray, horizon: int) -> FloatArray:
    """``values[t + 1] + ... + values[t + horizon]`` at every t (missing past the end)."""
    n = len(values)
    cumulative = np.r_[0.0, np.cumsum(values)]
    out = np.full(n, np.nan)
    if n > horizon:
        out[: n - horizon] = cumulative[horizon + 1 :] - cumulative[1 : n - horizon + 1]
    return out


class RollingRV(VolForecaster):
    """The mean RV of the last `window` periods."""

    def __init__(self, window: int) -> None:
        if window < 1:
            raise ValueError("the rolling window is at least one period")
        self.window = window
        self.name = f"rolling_{window}"

    def _fit(self, periods: pd.DataFrame) -> None:
        """Nothing to learn."""

    def _predict(self, periods: pd.DataFrame, horizon: int) -> FloatArray:
        return horizon * _trailing_mean(periods["rv"].to_numpy(np.float64), self.window)


class Ewma(VolForecaster):
    """RiskMetrics EWMA of squared period returns with a fixed lambda."""

    def __init__(self, lam: float) -> None:
        if not 0 < lam < 1:
            raise ValueError("lambda lies in (0, 1)")
        self.lam = lam
        self.name = f"ewma_{lam:g}"
        self.initial = float("nan")

    def _fit(self, periods: pd.DataFrame) -> None:
        squared = periods["ret"].to_numpy(np.float64) ** 2
        self.initial = float(np.nanmean(squared))

    def _predict(self, periods: pd.DataFrame, horizon: int) -> FloatArray:
        if not np.isfinite(self.initial):
            raise RuntimeError("fit the EWMA before predicting")
        squared = periods["ret"].to_numpy(np.float64) ** 2
        out = np.empty(len(squared))
        state = self.initial
        for t, value in enumerate(squared):
            if np.isfinite(value):
                state = self.lam * state + (1 - self.lam) * value
            out[t] = state  # the forecast of the next period, made after period t
        return horizon * out


class Har(VolForecaster):
    """HAR-RV with fixed components, one OLS per horizon on the training periods."""

    def __init__(self, components: Sequence[int], floor_share: float) -> None:
        if not components or any(c < 1 for c in components):
            raise ValueError("HAR components are at least one period")
        self.components = sorted(set(components))
        self.floor_share = floor_share
        self.name = "har"
        self._train_rv: FloatArray | None = None
        self._coefficients: dict[int, FloatArray] = {}

    def _design(self, rv: FloatArray) -> FloatArray:
        columns = [np.ones(len(rv))] + [_trailing_mean(rv, c) for c in self.components]
        return np.column_stack(columns)

    def _fit(self, periods: pd.DataFrame) -> None:
        self._train_rv = periods["rv"].to_numpy(np.float64).copy()
        self._coefficients = {}

    def coefficients(self, horizon: int) -> FloatArray:
        """The OLS coefficients (constant first) for `horizon`, fitted on the training periods."""
        if self._train_rv is None:
            raise RuntimeError("fit the HAR before predicting")
        if horizon not in self._coefficients:
            rv = self._train_rv
            x, y = self._design(rv), _future_sum(rv, horizon)
            usable = np.isfinite(y) & np.isfinite(x).all(axis=1)
            if usable.sum() <= x.shape[1]:
                raise ValueError(f"too few training periods for a HAR at horizon {horizon}")
            beta, *_ = np.linalg.lstsq(x[usable], y[usable], rcond=None)
            self._coefficients[horizon] = np.asarray(beta, dtype=np.float64)
        return self._coefficients[horizon]

    def _predict(self, periods: pd.DataFrame, horizon: int) -> FloatArray:
        beta = self.coefficients(horizon)
        assert self._train_rv is not None
        floor = self.floor_share * float(np.nanmean(self._train_rv)) * horizon
        forecast = self._design(periods["rv"].to_numpy(np.float64)) @ beta
        return np.where(np.isfinite(forecast), np.maximum(forecast, floor), np.nan)


class Deseasonalized(VolForecaster):
    """A forecaster on diurnally adjusted hourly periods (module docstring)."""

    def __init__(self, inner: VolForecaster, cfg: DiurnalConfig, bucket_minutes: int = 60) -> None:
        self.inner = inner
        self.cfg = cfg
        self.bucket_minutes = bucket_minutes
        self.name = inner.name
        self.factor: DiurnalFactor | None = None

    def _fit(self, periods: pd.DataFrame) -> None:
        index = pd.DatetimeIndex(periods.index)
        self.factor = DiurnalFactor.fit(
            periods["period_start"],
            index,
            periods["rv"].to_numpy(np.float64),
            self.cfg,
            bucket_minutes=self.bucket_minutes,
            train_end=index[-1],
        )
        self.inner.fit(self.adjust(periods))

    def adjust(self, periods: pd.DataFrame) -> pd.DataFrame:
        """`periods` with returns, RV, bipower and jumps divided by the fitted factor."""
        if self.factor is None:
            raise RuntimeError("fit before adjusting")
        f = self.factor.variance_factor(periods["period_start"])
        adjusted = periods.copy()
        adjusted["ret"] = periods["ret"].to_numpy(np.float64) / np.sqrt(f)
        for column in ("rv", "bv", "jump"):
            if column in adjusted:
                adjusted[column] = periods[column].to_numpy(np.float64) / f
        return adjusted

    def _predict(self, periods: pd.DataFrame, horizon: int) -> FloatArray:
        if self.factor is None:
            raise RuntimeError("fit before predicting")
        inner = self.inner.predict_variance(self.adjust(periods), horizon).to_numpy(np.float64)
        buckets = self.factor.buckets(periods["period_start"])
        ahead = {
            int(b): sum(
                self.factor.factors.get(n, 1.0) for n in self.factor.next_buckets(b, horizon)
            )
            for b in np.unique(buckets)
        }
        scale = np.array([ahead[int(b)] for b in buckets])
        return np.asarray(inner / horizon * scale, dtype=np.float64)


def benchmark_forecasters(cfg: VolatilityConfig, period: Timeframe) -> dict[str, Factory]:
    """Factories of the configured benchmarks by name; deseasonalized on hourly periods."""
    bench = cfg.benchmarks
    hourly = period == Timeframe("1h")
    components = bench.har_intraday_components if hourly else bench.har_components
    makers: dict[str, Factory] = {}
    for window in bench.rolling_windows:
        makers[f"rolling_{window}"] = partial(RollingRV, window)
    for lam in bench.ewma_lambdas:
        makers[f"ewma_{lam:g}"] = partial(Ewma, lam)
    makers["har"] = partial(Har, components, bench.variance_floor_share)
    if not hourly:
        return makers
    return {
        name: partial(_deseasonalized, make, cfg.realized.diurnal) for name, make in makers.items()
    }


def _deseasonalized(make: Factory, cfg: DiurnalConfig) -> VolForecaster:
    return Deseasonalized(make(), cfg)


__all__ = [
    "Deseasonalized",
    "Ewma",
    "Har",
    "RollingRV",
    "benchmark_forecasters",
]
