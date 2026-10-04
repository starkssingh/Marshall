"""Future realized volatility over the label window (TGT-003).

For a decision at time t and horizon h, the window is the forward return's (TGT-002): from the
entry fill (the first market-hours quote at or after ``t + latency`` of market time) to the exit
fill (the first such quote at or after ``t + h + latency``). Decisions while the market is closed,
and windows whose fills come later than ``max_fill_delay_s``, have no label.

The mid is sampled on a grid of ``sample_minutes`` of **market time** from the intended entry:

- the first point is the entry fill's mid;
- point k (k = 1 … n - 1, n = h / ``sample_minutes``) is the mid of the last market-hours quote at
  or before ``t + latency + k * sample_minutes``, never earlier than the entry fill (a gap in
  quotes carries the last mid forward);
- the last point is the exit fill's mid.

The value is ``sqrt(sum of squared log mid returns between the points)``, the realized volatility
over the window (not annualized, not per minute). Across a close the first point after the open
measures the gap, so a window that crosses the daily break or a weekend includes the overnight
return (unlike the intraday realized measures of VOL-002). ``label_end`` is the exit fill, the
last quote the value reads.

With ``vol_normalized`` each target also has a ``<name>_vol`` variant: the realized volatility
divided by ``scale = sigma_t * sqrt(h in market minutes)``, the interim sigma-hat known at t over
the horizon (TGT-002's EWMA; VOL-006 will replace it), i.e. realized over forecast volatility.

The volatility is measured on the mid: a target set of this kind takes ``price_refs: [mid]``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd
from pydantic import Field

from xq.core.config import TargetSetConfig
from xq.core.errors import ConfigError
from xq.data.calendar import MarketClock
from xq.targets.base import Lookahead, TargetKind, TargetSpec, market_horizon
from xq.targets.returns import (
    ExecutionParams,
    execution_lookahead,
    horizon_scale,
    interim_sigma_rate,
    kind_params,
    label_windows,
    market_rows,
    ns_values,
)

KIND = "realized_vol"
_MINUTE = pd.Timedelta(minutes=1)


class RealizedVolParams(ExecutionParams):
    """Parameters of a ``realized_vol`` target set."""

    sample_minutes: int = Field(gt=0)
    vol_normalized: bool = True


def realized_vol_params(params: Mapping[str, Any]) -> RealizedVolParams:
    """Validate a target set's ``params`` for the ``realized_vol`` kind."""
    return kind_params(RealizedVolParams, params, KIND)


def expand(definition: TargetSetConfig, trading_day: pd.Timedelta) -> list[TargetSpec]:
    """``tgt_rv_<horizon>`` (and ``..._vol``) for every horizon.

    Raises:
        ConfigError: if the price references are not ``[mid]`` or a horizon is not a whole number
            of sampling intervals.
    """
    params = realized_vol_params(definition.params)
    if list(definition.price_refs) != ["mid"]:
        raise ConfigError(f"{KIND} is measured on the mid: price_refs must be [mid]")
    step = params.sample_minutes * _MINUTE
    specs = []
    for label in definition.horizons:
        horizon = market_horizon(label, trading_day)
        if horizon % step != pd.Timedelta(0):
            raise ConfigError(
                f"{KIND} horizon {label} is not a whole number of {params.sample_minutes}-minute "
                "sampling intervals"
            )
        common = params.model_dump()
        specs.append(TargetSpec(f"tgt_rv_{label}", horizon, "mid", {**common, "normalized": False}))
        if params.vol_normalized:
            specs.append(
                TargetSpec(f"tgt_rv_{label}_vol", horizon, "mid", {**common, "normalized": True})
            )
    return specs


def sigma_rate(close: pd.Series, definition: TargetSetConfig, bar: pd.Timedelta) -> pd.Series:
    """The interim sigma-hat per square-root minute (TGT-002's causal EWMA)."""
    return interim_sigma_rate(close, realized_vol_params(definition.params).sigma_span_bars, bar)


def lookahead(definition: TargetSetConfig, trading_day: pd.Timedelta) -> Lookahead:
    """The longest horizon plus latency in market time, then the allowed fill delay."""
    return execution_lookahead(realized_vol_params(definition.params), definition, trading_day)


def compute(
    spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series, clock: MarketClock
) -> pd.DataFrame:
    """Realized volatility of `spec` at every decision time of `sigma` (module docstring)."""
    windows = label_windows(spec, quotes, sigma, clock)
    ok = windows.ok
    value = np.full(len(ok), np.nan)
    if ok.any():
        mid = (quotes["bid"].to_numpy(np.float64) + quotes["ask"].to_numpy(np.float64)) / 2
        rows = market_rows(windows.ts, clock)
        times = windows.ts[rows]
        entry = np.searchsorted(rows, windows.entry[ok])  # positions among market-hours quotes
        decisions = ns_values(windows.index)[ok]
        latency = pd.Timedelta(milliseconds=int(spec.params["execution_latency_ms"])).value
        step = pd.Timedelta(minutes=int(spec.params["sample_minutes"])).value
        points = [windows.entry[ok]]
        for k in range(1, int(spec.horizon.value // step)):
            grid = clock.advance(decisions, latency + k * step)
            last = np.searchsorted(times, grid, side="right") - 1
            points.append(rows[np.maximum(last, entry)])
        points.append(windows.exit[ok])
        log_mid = np.log(mid[np.column_stack(points)])
        value[ok] = np.sqrt(np.sum(np.diff(log_mid, axis=1) ** 2, axis=1))

    scale = np.full(len(ok), np.nan)
    if spec.params["normalized"]:
        scale = horizon_scale(sigma, spec.horizon)
        value = value / scale
    return windows.frame(value, scale, clock)


REALIZED_VOL = TargetKind(
    name=KIND,
    code_version=1,
    expand=expand,
    sigma=sigma_rate,
    compute=compute,
    lookahead=lookahead,
)
