"""Execution-aware forward returns (TGT-002), with trading-time horizons (ADR 0026).

For a decision at time t (a base bar's ``available_at``) and horizon h, times are counted on the
`MarketClock` — only market-open time passes, so the daily break, weekends and holidays are
skipped. ``1d`` is one regular trading day, 23 market hours (ADR 0032); ``4h`` is 4 market hours:

- a decision taken while the market is closed (at the 17:00 close itself, in the daily break, on a
  weekend or holiday) gets no label (ADR 0032);
- the intended entry is ``latency`` of market time after t, and the intended exit ``h + latency``
  after t;
- the entry fill is the first usable quote at or after the intended entry, the exit fill the first
  at or after the intended exit;
- ``long`` buys at the entry ask and sells at the exit bid: ``log(bid_exit / ask_entry)``;
  ``short`` sells at the entry bid and buys back at the exit ask: ``log(bid_entry / ask_exit)``;
  ``mid`` is the symmetric research variant ``log(mid_exit / mid_entry)``;
- ``label_start`` is the entry fill's time and ``label_end`` the exit fill's time;
  ``crosses_close`` is True when a market close lies between them (the overnight or weekend gap
  is part of the holding period);
- fill delays are wall-clock: if either fill comes more than ``max_fill_delay_s`` after its
  intended time (missing quotes, an excluded day, the end of the data), there is no label and the
  value is missing; ``fill_delay_s`` is the larger of the two fills' delays.

Never the signal bar's close and never mid for a trade: the spread is paid on both legs.

With ``vol_normalized`` each target also has a ``<name>_vol`` variant: the return divided by
``scale = sigma_t * sqrt(h in market minutes)``, where ``sigma_t`` is the interim sigma-hat known
at t — an EWMA of squared 1-bar log returns of the base close (span ``sigma_span_bars``),
expressed per square-root minute. VOL-006 will replace this estimate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from xq.core.config import TargetSetConfig
from xq.core.errors import ConfigError
from xq.data.calendar import NAT_NS, MarketClock
from xq.datasets.primitives import ewma_volatility, log_returns
from xq.targets.base import Lookahead, TargetKind, TargetSpec, market_horizon

_MINUTE = pd.Timedelta(minutes=1)


class ForwardReturnParams(BaseModel):
    """Parameters of a ``forward_return`` target set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_latency_ms: int = Field(ge=0)
    max_fill_delay_s: float = Field(gt=0)
    sigma_span_bars: int = Field(gt=1)
    vol_normalized: bool = True


def forward_return_params(params: Mapping[str, Any]) -> ForwardReturnParams:
    """Validate a target set's ``params`` for the ``forward_return`` kind."""
    try:
        return ForwardReturnParams.model_validate(dict(params))
    except ValidationError as exc:
        raise ConfigError(f"invalid forward_return params:\n{exc}") from exc


def expand(definition: TargetSetConfig, trading_day: pd.Timedelta) -> list[TargetSpec]:
    """``fwd_ret_<ref>_<horizon>`` (and ``..._vol``) for every horizon and price reference."""
    params = forward_return_params(definition.params)
    specs = []
    for label in definition.horizons:
        horizon = market_horizon(label, trading_day)
        for ref in definition.price_refs:
            base = f"fwd_ret_{ref}_{label}"
            common = params.model_dump()
            specs.append(TargetSpec(base, horizon, ref, {**common, "normalized": False}))
            if params.vol_normalized:
                specs.append(
                    TargetSpec(f"{base}_vol", horizon, ref, {**common, "normalized": True})
                )
    return specs


def sigma_rate(close: pd.Series, definition: TargetSetConfig, bar: pd.Timedelta) -> pd.Series:
    """Interim sigma-hat per square-root minute at each decision time (causal EWMA)."""
    params = forward_return_params(definition.params)
    per_bar = ewma_volatility(log_returns(close), span=params.sigma_span_bars, min_periods=2)
    rate: pd.Series = per_bar / float(np.sqrt(bar / _MINUTE))
    return rate


def lookahead(definition: TargetSetConfig, trading_day: pd.Timedelta) -> Lookahead:
    """The longest horizon plus latency in market time, then the allowed fill delay."""
    params = forward_return_params(definition.params)
    longest = max(market_horizon(h, trading_day) for h in definition.horizons)
    return Lookahead(
        market=longest + pd.Timedelta(milliseconds=params.execution_latency_ms),
        wall=pd.Timedelta(seconds=params.max_fill_delay_s),
    )


def compute(
    spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series, clock: MarketClock
) -> pd.DataFrame:
    """Forward return of `spec` at every decision time of `sigma` (see the module docstring)."""
    t = _ns(pd.DatetimeIndex(sigma.index))
    ts = _ns(pd.DatetimeIndex(quotes["ts_utc"])) if len(quotes) else np.array([], np.int64)
    bid = quotes["bid"].to_numpy(dtype=np.float64)
    ask = quotes["ask"].to_numpy(dtype=np.float64)
    latency = pd.Timedelta(milliseconds=int(spec.params["execution_latency_ms"])).value
    delay = pd.Timedelta(seconds=float(spec.params["max_fill_delay_s"])).value

    entry, entry_ok, entry_late = _fill(ts, clock.advance(t, latency), delay)
    exit_, exit_ok, exit_late = _fill(ts, clock.advance(t, spec.horizon.value + latency), delay)
    ok = entry_ok & exit_ok & clock.is_open(t)  # no label for decisions while closed
    fill_delay = np.where(ok, np.maximum(entry_late, exit_late) / 1e9, np.nan)
    value = np.full(len(t), np.nan)
    if ok.any():
        e, x = entry[ok], exit_[ok]
        if spec.price_ref == "long":
            value[ok] = np.log(bid[x] / ask[e])
        elif spec.price_ref == "short":
            value[ok] = np.log(bid[e] / ask[x])
        else:
            value[ok] = np.log((bid[x] + ask[x]) / (bid[e] + ask[e]))

    scale = np.full(len(t), np.nan)
    if spec.params["normalized"]:
        rate = sigma.to_numpy(dtype=np.float64)
        scale = rate * np.sqrt(spec.horizon / _MINUTE)
        scale = np.where(scale > 0, scale, np.nan)
        value = value / scale

    index = pd.DatetimeIndex(sigma.index)
    stamps = np.full(len(t), NAT_NS, dtype=np.int64)  # NaT where no label
    ends = stamps.copy()
    stamps[ok] = ts[entry[ok]]
    ends[ok] = ts[exit_[ok]]
    crosses = np.zeros(len(t), dtype=bool)
    crosses[ok] = clock.crosses_close(stamps[ok], ends[ok])
    return pd.DataFrame(
        {
            "value": value,
            "label_start": pd.to_datetime(stamps, unit="ns", utc=True),
            "label_end": pd.to_datetime(ends, unit="ns", utc=True),
            "crosses_close": crosses,
            "scale": scale,
            "fill_delay_s": fill_delay,
        },
        index=index,
    )


def _fill(
    ts: npt.NDArray[np.int64], intended: npt.NDArray[np.int64], delay: int
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.bool_], npt.NDArray[np.int64]]:
    """First quote at or after each intended time: its index, whether it is timely, its delay.

    An intended time of `NAT_NS` (beyond the market clock) has no fill. The delay (nanoseconds) is
    0 where no quote follows.
    """
    index = np.searchsorted(ts, intended, side="left")
    found = (index < len(ts)) & (intended != NAT_NS)
    late = np.zeros(len(intended), dtype=np.int64)
    late[found] = ts[index[found]] - intended[found]
    timely = found & (late <= delay)
    return index.astype(np.int64), timely, late


def _ns(index: pd.DatetimeIndex) -> npt.NDArray[np.int64]:
    values: npt.NDArray[np.int64] = (
        index.tz_convert("UTC").as_unit("ns").to_numpy(dtype="datetime64[ns]").view(np.int64)
    )
    return values


FORWARD_RETURN = TargetKind(
    name="forward_return",
    code_version=4,
    expand=expand,
    sigma=sigma_rate,
    compute=compute,
    lookahead=lookahead,
)
