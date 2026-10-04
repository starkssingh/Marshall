"""Maximum favourable and adverse excursion over the horizon, in sigma units (TGT-004).

The window is the forward return's (TGT-002): entry fill at the first market-hours quote at or
after ``t + latency`` of market time, exit fill at the first such quote at or after
``t + h + latency``; decisions while the market is closed, and late fills, get no label. The
**path** is every market-hours quote after the entry fill up to and including the exit fill (a
quote while the market is closed is never tradable, ADR 0050). Each path quote is marked on the
**exit side** of the position, as if it were closed there:

- ``long`` buys at the entry ask; a path quote is worth ``r = log(bid / ask_entry)``;
- ``short`` sells at the entry bid; a path quote is worth ``r = log(bid_entry / ask)``.

``mfe = max r`` (the best close-out over the path) and ``mae = -min r`` (the worst, as a loss:
positive when the position was under water). Both include the spread, so ``mae`` is at least the
entry spread unless the price moved favourably at once. Each is divided by
``scale = sigma_t * sqrt(h in market minutes)``, the interim sigma-hat known at t over the horizon
(TGT-002's EWMA), so the values are in sigma units; no fixed-dollar quantity appears.
``label_end`` is the exit fill, the last quote read. Target names: ``tgt_mfe_<ref>_<h>`` and
``tgt_mae_<ref>_<h>`` for ``long`` and ``short`` (the mid has no exit side).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

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
)

KIND = "excursion"
MEASURES = ("mfe", "mae")


def excursion_params(params: Mapping[str, Any]) -> ExecutionParams:
    """Validate a target set's ``params`` for the ``excursion`` kind."""
    return kind_params(ExecutionParams, params, KIND)


def expand(definition: TargetSetConfig, trading_day: pd.Timedelta) -> list[TargetSpec]:
    """``tgt_mfe_<ref>_<h>`` and ``tgt_mae_<ref>_<h>`` for every horizon and side.

    Raises:
        ConfigError: if a price reference is ``mid`` (it has no exit side).
    """
    params = excursion_params(definition.params)
    if "mid" in definition.price_refs:
        raise ConfigError(f"{KIND} is marked on the exit side: price_refs may be long and short")
    return [
        TargetSpec(
            f"tgt_{measure}_{ref}_{label}",
            market_horizon(label, trading_day),
            ref,
            {**params.model_dump(), "measure": measure},
        )
        for label in definition.horizons
        for ref in definition.price_refs
        for measure in MEASURES
    ]


def sigma_rate(close: pd.Series, definition: TargetSetConfig, bar: pd.Timedelta) -> pd.Series:
    """The interim sigma-hat per square-root minute (TGT-002's causal EWMA)."""
    return interim_sigma_rate(close, excursion_params(definition.params).sigma_span_bars, bar)


def lookahead(definition: TargetSetConfig, trading_day: pd.Timedelta) -> Lookahead:
    """The longest horizon plus latency in market time, then the allowed fill delay."""
    return execution_lookahead(excursion_params(definition.params), definition, trading_day)


def compute(
    spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series, clock: MarketClock
) -> pd.DataFrame:
    """The excursion of `spec` at every decision time of `sigma` (module docstring)."""
    windows = label_windows(spec, quotes, sigma, clock)
    ok = windows.ok
    scale = horizon_scale(sigma, spec.horizon)
    value = np.full(len(ok), np.nan)
    if ok.any():
        bid = quotes["bid"].to_numpy(np.float64)
        ask = quotes["ask"].to_numpy(np.float64)
        rows = market_rows(windows.ts, clock)
        first, last = path_bounds(rows, windows.entry[ok], windows.exit[ok])
        long = spec.price_ref == "long"
        marks = (bid if long else ask)[rows]
        high = segment_reduce(np.maximum, marks, first, last)
        low = segment_reduce(np.minimum, marks, first, last)
        if long:
            entry_ask = ask[windows.entry[ok]]
            best, worst = np.log(high / entry_ask), np.log(low / entry_ask)
        else:
            entry_bid = bid[windows.entry[ok]]
            best, worst = np.log(entry_bid / low), np.log(entry_bid / high)
        value[ok] = best if spec.params["measure"] == "mfe" else -worst
    return windows.frame(value / scale, scale, clock)


def path_bounds(
    rows: npt.NDArray[np.int64], entry: npt.NDArray[np.int64], exit_: npt.NDArray[np.int64]
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    """Positions among the market-hours `rows` of each path: after the entry fill up to and
    including the exit fill (the exit fill alone when nothing lies between)."""
    start = np.searchsorted(rows, entry)
    end = np.searchsorted(rows, exit_)
    first: npt.NDArray[np.int64] = np.where(end > start, start + 1, end).astype(np.int64)
    return first, end.astype(np.int64)


def segment_reduce(
    ufunc: np.ufunc,
    values: npt.NDArray[np.float64],
    first: npt.NDArray[np.int64],
    last: npt.NDArray[np.int64],
) -> npt.NDArray[np.float64]:
    """``ufunc`` over ``values[first[i] : last[i] + 1]`` for each i (segments may overlap)."""
    padded = np.append(values, np.nan)  # reduceat needs every index inside the array
    bounds = np.empty(2 * len(first), dtype=np.int64)
    bounds[0::2], bounds[1::2] = first, last + 1
    reduced: npt.NDArray[np.float64] = ufunc.reduceat(padded, bounds)[0::2]
    return reduced


EXCURSION = TargetKind(
    name=KIND,
    code_version=1,
    expand=expand,
    sigma=sigma_rate,
    compute=compute,
    lookahead=lookahead,
)
