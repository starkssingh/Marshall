"""Triple-barrier labels with pessimistic same-bar handling (TGT-005).

The window is the forward return's (TGT-002): entry fill at the first market-hours quote at or
after ``t + latency`` of market time, the **vertical barrier** at the exit fill, the first such
quote at or after ``t + h + latency``. Decisions while the market is closed, late fills and
decisions without a sigma-hat get no label. With ``s = sigma_t * sqrt(h in market minutes)``,
the interim sigma-hat known at t over the horizon (TGT-002's EWMA):

- the path is every market-hours quote after the entry fill up to and including the exit fill,
  marked on the exit side as a close-out return: ``long`` ``g = log(bid / ask_entry)``, ``short``
  ``g = log(bid_entry / ask)`` (side-specific; the spread is paid);
- the **take-profit** is hit when ``g >= tp_sigmas * s``, the **stop** when
  ``g <= -sl_sigmas * s`` (barriers in sigma units, never dollars);
- the label is ``+1`` (take-profit first), ``-1`` (stop first) or ``0`` (neither before the
  vertical barrier).

**Resolution.** With ``resolution: tick`` every quote is a path point: one price cannot be on both
sides of the entry, so the first barrier touched is known exactly. With a bar length (``1m``,
``5m``, ...) the path is read as bars of that length on the UTC grid, as when only bars are
available: a bar touching both barriers is resolved **pessimistically** to the stop and flagged
ambiguous (the event tier's bar mode does the same, ADR 0049); the hit is placed at the bar's last
path quote, the latest instant the bar's high and low depend on.

Each (horizon, side) gives three targets with the same window:

- ``tgt_tb_<ref>_<h>``: the label;
- ``tgt_tb_<ref>_<h>_t``: market minutes from the entry fill to the hit (or the vertical barrier);
- ``tgt_tb_<ref>_<h>_amb``: 1 when the label came from an ambiguous bar, else 0.

``label_end`` is the hit quote (or the exit fill): purging by it removes exactly the data the
label read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import Field, field_validator

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

KIND = "triple_barrier"
#: Measure -> name suffix of the three targets of one window.
MEASURES = {"label": "", "time": "_t", "ambiguous": "_amb"}
TICK = "tick"
_MINUTE_NS = 60_000_000_000


class BarrierParams(ExecutionParams):
    """Parameters of a ``triple_barrier`` target set."""

    tp_sigmas: float = Field(gt=0)
    sl_sigmas: float = Field(gt=0)
    resolution: str = TICK

    @field_validator("resolution")
    @classmethod
    def _resolution(cls, value: str) -> str:
        if value != TICK:
            try:
                length = pd.Timedelta(value)
            except ValueError:
                raise ValueError(
                    f"resolution must be {TICK!r} or a bar length, not {value!r}"
                ) from None
            if length <= pd.Timedelta(0):
                raise ValueError("a bar resolution must be positive")
        return value


def barrier_params(params: Mapping[str, Any]) -> BarrierParams:
    """Validate a target set's ``params`` for the ``triple_barrier`` kind."""
    return kind_params(BarrierParams, params, KIND)


def expand(definition: TargetSetConfig, trading_day: pd.Timedelta) -> list[TargetSpec]:
    """The label, time-to-hit and ambiguity targets of every horizon and side.

    Raises:
        ConfigError: if a price reference is ``mid`` (barriers are side-specific).
    """
    params = barrier_params(definition.params)
    if "mid" in definition.price_refs:
        raise ConfigError(f"{KIND} labels are side-specific: price_refs may be long and short")
    return [
        TargetSpec(
            f"tgt_tb_{ref}_{label}{suffix}",
            market_horizon(label, trading_day),
            ref,
            {**params.model_dump(), "measure": measure},
        )
        for label in definition.horizons
        for ref in definition.price_refs
        for measure, suffix in MEASURES.items()
    ]


def sigma_rate(close: pd.Series, definition: TargetSetConfig, bar: pd.Timedelta) -> pd.Series:
    """The interim sigma-hat per square-root minute (TGT-002's causal EWMA)."""
    return interim_sigma_rate(close, barrier_params(definition.params).sigma_span_bars, bar)


def lookahead(definition: TargetSetConfig, trading_day: pd.Timedelta) -> Lookahead:
    """The longest horizon plus latency in market time, then the allowed fill delay."""
    return execution_lookahead(barrier_params(definition.params), definition, trading_day)


def compute(
    spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series, clock: MarketClock
) -> pd.DataFrame:
    """The `spec` measure of the triple barrier at every decision time of `sigma`.

    A window needs a timely entry fill and a sigma-hat. A barrier touched before the intended
    exit labels it whether or not the exit fill exists; only the vertical outcome needs the exit
    fill (so a label never depends on quotes after the one that decided it).
    """
    windows = label_windows(spec, quotes, sigma, clock)
    scale = horizon_scale(sigma, spec.horizon)
    candidate = windows.entry_ok & np.isfinite(scale)
    value = np.full(len(candidate), np.nan)
    end = windows.exit.copy()
    ok = np.zeros(len(candidate), dtype=bool)
    delay = np.where(windows.exit_ok, windows.fill_delay_s, windows.entry_delay_s)
    if candidate.any():
        bid = quotes["bid"].to_numpy(np.float64)
        ask = quotes["ask"].to_numpy(np.float64)
        rows = market_rows(windows.ts, clock)
        times = windows.ts[rows]
        long = spec.price_ref == "long"
        marks = (bid if long else ask)[rows]
        entry_price = (ask if long else bid)[windows.entry[candidate]]
        start = np.searchsorted(rows, windows.entry[candidate])
        timely_exit = windows.exit_ok[candidate]
        before_exit = np.searchsorted(times, windows.intended_exit[candidate], side="left") - 1
        last = np.where(timely_exit, np.searchsorted(rows, windows.exit[candidate]), before_exit)
        first = np.where(timely_exit & (last == start), start, start + 1)
        resolution = str(spec.params["resolution"])
        outcome = barrier_outcomes(
            marks,
            times,
            first.astype(np.int64),
            last.astype(np.int64),
            entry_price,
            scale[candidate],
            long=long,
            tp_sigmas=float(spec.params["tp_sigmas"]),
            sl_sigmas=float(spec.params["sl_sigmas"]),
            bar_ns=None if resolution == TICK else pd.Timedelta(resolution).value,
        )
        # labelled: a barrier was touched, or the vertical barrier's exit fill is there
        decided = (outcome.label != 0) | timely_exit
        ok[candidate] = decided
        where = np.flatnonzero(candidate)[decided]
        hit = rows[outcome.hit[decided]]
        end[where] = hit
        measure = spec.params["measure"]
        if measure == "label":
            value[where] = outcome.label[decided]
        elif measure == "ambiguous":
            value[where] = outcome.ambiguous[decided].astype(np.float64)
        else:
            entered = clock.elapsed(windows.ts[windows.entry[where]])
            value[where] = (clock.elapsed(windows.ts[hit]) - entered) / _MINUTE_NS
    return windows.frame(value, scale, clock, end=end, ok=ok, fill_delay_s=delay)


@dataclass(frozen=True)
class BarrierOutcome:
    """Per window: the label (+1, -1, 0), the path position of the hit and the ambiguity flag."""

    label: npt.NDArray[np.float64]
    hit: npt.NDArray[np.int64]
    ambiguous: npt.NDArray[np.bool_]


def barrier_outcomes(
    marks: npt.NDArray[np.float64],
    times: npt.NDArray[np.int64],
    first: npt.NDArray[np.int64],
    last: npt.NDArray[np.int64],
    entry_price: npt.NDArray[np.float64],
    scale: npt.NDArray[np.float64],
    *,
    long: bool,
    tp_sigmas: float,
    sl_sigmas: float,
    bar_ns: int | None,
) -> BarrierOutcome:
    """The first barrier each path ``marks[first[i] : last[i] + 1]`` touches (module docstring).

    `marks` are exit-side prices at `times`; `bar_ns` None reads every quote, otherwise bars of
    that length on the UTC grid, a bar touching both barriers resolving to the stop (ambiguous).
    An empty path (``last < first``) touches nothing. Where nothing is touched the label is 0 and
    the hit is `last`.
    """
    n = len(first)
    label = np.zeros(n, dtype=np.float64)
    hit = last.copy()
    ambiguous = np.zeros(n, dtype=bool)
    for i in range(n):
        if last[i] < first[i]:
            continue
        path = marks[first[i] : last[i] + 1]
        gain = np.log(path / entry_price[i]) if long else np.log(entry_price[i] / path)
        up = gain >= tp_sigmas * scale[i]
        down = gain <= -sl_sigmas * scale[i]
        if not (up.any() or down.any()):
            continue  # the vertical barrier
        if bar_ns is None:
            k = int(np.argmax(up | down))
            label[i] = 1.0 if up[k] else -1.0
            hit[i] = first[i] + k
            continue
        bars = times[first[i] : last[i] + 1] // bar_ns
        starts = np.flatnonzero(np.r_[True, bars[1:] != bars[:-1]])
        bar_up = np.logical_or.reduceat(up, starts)
        bar_down = np.logical_or.reduceat(down, starts)
        b = int(np.argmax(bar_up | bar_down))
        both = bool(bar_up[b] and bar_down[b])
        label[i] = -1.0 if bar_down[b] else 1.0  # pessimistic: a bar touching both is a stop
        ambiguous[i] = both
        bar_end = starts[b + 1] - 1 if b + 1 < len(starts) else len(path) - 1
        hit[i] = first[i] + bar_end
    return BarrierOutcome(label, hit, ambiguous)


TRIPLE_BARRIER = TargetKind(
    name=KIND,
    code_version=1,
    expand=expand,
    sigma=sigma_rate,
    compute=compute,
    lookahead=lookahead,
)
