"""Execution-aware forward returns (TGT-002), with trading-time horizons (ADR 0026).

For a decision at time t (a base bar's ``available_at``) and horizon h, times are counted on the
`MarketClock` — only market-open time passes, so the daily break, weekends and holidays are
skipped. ``1d`` is one regular trading day, 23 market hours (ADR 0032); ``4h`` is 4 market hours:

- a decision taken while the market is closed (at the 17:00 close itself, in the daily break, on a
  weekend or holiday) gets no label (ADR 0032);
- the intended entry is ``latency`` of market time after t, and the intended exit ``h + latency``
  after t;
- the entry fill is the first usable quote at or after the intended entry that lies in market
  hours, the exit fill the first such quote at or after the intended exit — a quote while the
  market is closed (a stray quote in the daily break) is never a fill (ADR 0050, code version 5);
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
from dataclasses import dataclass
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


class ExecutionParams(BaseModel):
    """Execution parameters every execution-aware target kind shares (TGT-002 … TGT-006)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_latency_ms: int = Field(ge=0)
    max_fill_delay_s: float = Field(gt=0)
    sigma_span_bars: int = Field(gt=1)


class ForwardReturnParams(ExecutionParams):
    """Parameters of a ``forward_return`` target set."""

    vol_normalized: bool = True


def kind_params[P: BaseModel](model: type[P], params: Mapping[str, Any], kind: str) -> P:
    """Validate a target set's ``params`` for `kind` with `model`."""
    try:
        return model.model_validate(dict(params))
    except ValidationError as exc:
        raise ConfigError(f"invalid {kind} params:\n{exc}") from exc


def forward_return_params(params: Mapping[str, Any]) -> ForwardReturnParams:
    """Validate a target set's ``params`` for the ``forward_return`` kind."""
    return kind_params(ForwardReturnParams, params, "forward_return")


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
    return interim_sigma_rate(close, params.sigma_span_bars, bar)


def interim_sigma_rate(close: pd.Series, span: int, bar: pd.Timedelta) -> pd.Series:
    """EWMA (span `span`) of squared 1-bar log returns of `close`, per square-root minute."""
    per_bar = ewma_volatility(log_returns(close), span=span, min_periods=2)
    rate: pd.Series = per_bar / float(np.sqrt(bar / _MINUTE))
    return rate


def lookahead(definition: TargetSetConfig, trading_day: pd.Timedelta) -> Lookahead:
    """The longest horizon plus latency in market time, then the allowed fill delay."""
    return execution_lookahead(forward_return_params(definition.params), definition, trading_day)


def execution_lookahead(
    params: ExecutionParams, definition: TargetSetConfig, trading_day: pd.Timedelta
) -> Lookahead:
    """The longest horizon plus latency in market time, then the allowed fill delay."""
    longest = max(market_horizon(h, trading_day) for h in definition.horizons)
    return Lookahead(
        market=longest + pd.Timedelta(milliseconds=params.execution_latency_ms),
        wall=pd.Timedelta(seconds=params.max_fill_delay_s),
    )


@dataclass(frozen=True)
class LabelWindows:
    """Entry and exit fills of every decision's label window (see the module docstring).

    Attributes:
        index: The decision times.
        ts: Quote times (UTC nanoseconds), sorted.
        entry, exit: Rows of the entry and exit fill quotes (meaningful where `ok`).
        ok: Labelled: decided while the market is open, both fills found within the delay.
        fill_delay_s: The later fill's delay after its intended time (NaN without a label).
    """

    index: pd.DatetimeIndex
    ts: npt.NDArray[np.int64]
    entry: npt.NDArray[np.int64]
    exit: npt.NDArray[np.int64]
    ok: npt.NDArray[np.bool_]
    fill_delay_s: npt.NDArray[np.float64]

    def frame(
        self,
        value: npt.NDArray[np.float64],
        scale: npt.NDArray[np.float64],
        clock: MarketClock,
        *,
        end: npt.NDArray[np.int64] | None = None,
        ok: npt.NDArray[np.bool_] | None = None,
    ) -> pd.DataFrame:
        """The `VALUE_COLUMNS` frame: ``label_start`` at the entry fill, ``label_end`` at the
        quote row `end` (default the exit fill), both NaT and `value` NaN outside `ok`."""
        ok = self.ok if ok is None else ok
        end = self.exit if end is None else end
        stamps = np.full(len(self.index), NAT_NS, dtype=np.int64)  # NaT where no label
        ends = stamps.copy()
        stamps[ok] = self.ts[self.entry[ok]]
        ends[ok] = self.ts[end[ok]]
        crosses = np.zeros(len(self.index), dtype=bool)
        crosses[ok] = clock.crosses_close(stamps[ok], ends[ok])
        return pd.DataFrame(
            {
                "value": np.where(ok, value, np.nan),
                "label_start": pd.to_datetime(stamps, unit="ns", utc=True),
                "label_end": pd.to_datetime(ends, unit="ns", utc=True),
                "crosses_close": crosses,
                "scale": scale,
                "fill_delay_s": np.where(ok, self.fill_delay_s, np.nan),
            },
            index=self.index,
        )


def label_windows(
    spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series, clock: MarketClock
) -> LabelWindows:
    """Entry and exit fills of `spec` (its latency, horizon and fill delay) at every decision."""
    index = pd.DatetimeIndex(sigma.index)
    t = ns_values(index)
    ts = ns_values(pd.DatetimeIndex(quotes["ts_utc"])) if len(quotes) else np.array([], np.int64)
    latency = pd.Timedelta(milliseconds=int(spec.params["execution_latency_ms"])).value
    delay = pd.Timedelta(seconds=float(spec.params["max_fill_delay_s"])).value
    entry, entry_ok, entry_late = first_fill(ts, clock.advance(t, latency), delay, clock)
    exit_, exit_ok, exit_late = first_fill(
        ts, clock.advance(t, spec.horizon.value + latency), delay, clock
    )
    ok = entry_ok & exit_ok & clock.is_open(t)  # no label for decisions while closed
    fill_delay = np.where(ok, np.maximum(entry_late, exit_late) / 1e9, np.nan)
    return LabelWindows(index, ts, entry, exit_, ok, fill_delay)


def horizon_scale(sigma: pd.Series, horizon: pd.Timedelta) -> npt.NDArray[np.float64]:
    """``sigma_t * sqrt(h in market minutes)``, NaN where it is not positive."""
    scale = sigma.to_numpy(dtype=np.float64) * np.sqrt(horizon / _MINUTE)
    out: npt.NDArray[np.float64] = np.where(scale > 0, scale, np.nan)
    return out


def market_rows(ts: npt.NDArray[np.int64], clock: MarketClock) -> npt.NDArray[np.int64]:
    """Rows of the quotes inside market hours on the clock (the only tradable quotes)."""
    usable = np.zeros(len(ts), dtype=bool)
    inside = (ts >= clock.covered_from) & (ts < clock.covered_to)
    if inside.any():
        usable[inside] = clock.is_open(ts[inside])
    return np.flatnonzero(usable).astype(np.int64)


def compute(
    spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series, clock: MarketClock
) -> pd.DataFrame:
    """Forward return of `spec` at every decision time of `sigma` (see the module docstring)."""
    windows = label_windows(spec, quotes, sigma, clock)
    bid = quotes["bid"].to_numpy(dtype=np.float64)
    ask = quotes["ask"].to_numpy(dtype=np.float64)
    ok = windows.ok
    value = np.full(len(ok), np.nan)
    if ok.any():
        e, x = windows.entry[ok], windows.exit[ok]
        value[ok] = side_return(spec.price_ref, bid, ask, e, x)

    scale = np.full(len(ok), np.nan)
    if spec.params["normalized"]:
        scale = horizon_scale(sigma, spec.horizon)
        value = value / scale
    return windows.frame(value, scale, clock)


def side_return(
    ref: str,
    bid: npt.NDArray[np.float64],
    ask: npt.NDArray[np.float64],
    entry: npt.NDArray[np.int64],
    exit_: npt.NDArray[np.int64],
) -> npt.NDArray[np.float64]:
    """Log return of `ref` filled at rows `entry` and `exit_`: long ask to bid, short bid to ask,
    mid to mid."""
    if ref == "long":
        out: npt.NDArray[np.float64] = np.log(bid[exit_] / ask[entry])
    elif ref == "short":
        out = np.log(bid[entry] / ask[exit_])
    else:
        out = np.log((bid[exit_] + ask[exit_]) / (bid[entry] + ask[entry]))
    return out


def first_fill(
    ts: npt.NDArray[np.int64], intended: npt.NDArray[np.int64], delay: int, clock: MarketClock
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.bool_], npt.NDArray[np.int64]]:
    """First market-hours quote at or after each intended time: its row, timeliness and delay.

    A quote while the market is closed is never a fill quote (ADR 0050). An intended time of
    `NAT_NS` (beyond the market clock) has no fill. The delay (nanoseconds) is 0 where no quote
    follows.
    """
    index, found = clock.first_open(ts, intended)
    late = np.zeros(len(intended), dtype=np.int64)
    late[found] = ts[index[found]] - intended[found]
    timely = found & (late <= delay)
    return index, timely, late


def ns_values(index: pd.DatetimeIndex) -> npt.NDArray[np.int64]:
    """UTC nanoseconds of a tz-aware index."""
    values: npt.NDArray[np.int64] = (
        index.tz_convert("UTC").as_unit("ns").to_numpy(dtype="datetime64[ns]").view(np.int64)
    )
    return values


FORWARD_RETURN = TargetKind(
    name="forward_return",
    code_version=5,
    expand=expand,
    sigma=sigma_rate,
    compute=compute,
    lookahead=lookahead,
)
