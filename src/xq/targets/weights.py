"""Derived labels, label concurrency and average-uniqueness sample weights (TGT-006).

**Derived labels** (kind ``derived_label``) come from the forward returns of TGT-002 (the same
windows: entry and exit fills after the latency, market time, ``label_end`` at the exit fill):

- ``tgt_sign_<h>``: the sign of the mid return (-1, 0 or +1);
- ``tgt_big_<h>``: 1 when ``|mid return| > big_move_sigmas * s``, else 0, where
  ``s = sigma_t * sqrt(h in market minutes)`` is the interim sigma-hat known at t over the horizon;
- ``tgt_trade_<ref>_<h>`` (``long``, ``short``): 1 when the side's execution-aware return (the
  spread already paid by filling at the ask and the bid) exceeds the rest of the round trip's
  costs, else 0 — the trade/no-trade label. Those costs, in basis points of the price, mirror the
  provisional placeholder cost model (``config/costs/placeholder.yaml``, BT-001):
  commission ``2 * commission_usd_per_oz_per_side / entry mid``; slippage
  ``2 * (slippage_fixed_bps + slippage_sigma_multiple * sigma_1m)`` with ``sigma_1m`` the
  sigma-hat known at t over one market minute (the session multipliers around the rollover and
  releases are left out); financing for every market close the label holds over,
  ``rate / day_count`` per night on the side's rate, three nights on ``triple_weekday``.

The price references choose the labels: ``mid`` gives the sign and big-move labels, ``long`` and
``short`` the trade labels.

**Concurrency and uniqueness** (`label_uniqueness`). Each label occupies ``[label_start,
label_end)``, measured in market time when a `MarketClock` is given (closed periods do not count).
The concurrency at an instant is the number of labels occupying it. A label's **average
uniqueness** is the time-average of ``1 / concurrency`` over its interval: 1 for a label that
overlaps no other, ``1/k`` for k identical labels. Average-uniqueness **sample weights**
(`uniqueness_weights`) scale it to a mean of 1 over the labels passed, so they are computed within
a training set, never across folds. A label's uniqueness depends on every label overlapping it,
so it is known only at ``weight_end``, the latest ``label_end`` among them; that is the end to
purge by when weights are computed over labels that reach into a later window.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import Field

from xq.core.config import WEEKDAYS, TargetSetConfig
from xq.data.calendar import NAT_NS, MarketClock
from xq.targets.base import Lookahead, TargetKind, TargetSpec, market_horizon
from xq.targets.returns import (
    ExecutionParams,
    execution_lookahead,
    horizon_scale,
    interim_sigma_rate,
    kind_params,
    label_windows,
    ns_values,
    side_return,
)

KIND = "derived_label"
_BPS = 1e-4
_MINUTE = pd.Timedelta(minutes=1)
NY = "America/New_York"


class DerivedLabelParams(ExecutionParams):
    """Parameters of a ``derived_label`` target set (module docstring)."""

    big_move_sigmas: float = Field(gt=0)
    commission_usd_per_oz_per_side: float = Field(ge=0)
    slippage_fixed_bps: float = Field(ge=0)
    slippage_sigma_multiple: float = Field(ge=0)
    financing_long_annual_pct: float
    financing_short_annual_pct: float
    financing_day_count: int = Field(gt=0)
    triple_weekday: Literal["mon", "tue", "wed", "thu", "fri"]


def derived_label_params(params: Mapping[str, Any]) -> DerivedLabelParams:
    """Validate a target set's ``params`` for the ``derived_label`` kind."""
    return kind_params(DerivedLabelParams, params, KIND)


def expand(definition: TargetSetConfig, trading_day: pd.Timedelta) -> list[TargetSpec]:
    """Sign and big-move labels for ``mid``, trade labels for ``long`` and ``short``."""
    params = derived_label_params(definition.params).model_dump()
    specs = []
    for label in definition.horizons:
        horizon = market_horizon(label, trading_day)
        for ref in definition.price_refs:
            if ref == "mid":
                specs.append(
                    TargetSpec(f"tgt_sign_{label}", horizon, ref, {**params, "derive": "sign"})
                )
                specs.append(
                    TargetSpec(f"tgt_big_{label}", horizon, ref, {**params, "derive": "big"})
                )
            else:
                specs.append(
                    TargetSpec(
                        f"tgt_trade_{ref}_{label}", horizon, ref, {**params, "derive": "trade"}
                    )
                )
    return specs


def sigma_rate(close: pd.Series, definition: TargetSetConfig, bar: pd.Timedelta) -> pd.Series:
    """The interim sigma-hat per square-root minute (TGT-002's causal EWMA)."""
    return interim_sigma_rate(close, derived_label_params(definition.params).sigma_span_bars, bar)


def lookahead(definition: TargetSetConfig, trading_day: pd.Timedelta) -> Lookahead:
    """The longest horizon plus latency in market time, then the allowed fill delay."""
    return execution_lookahead(derived_label_params(definition.params), definition, trading_day)


def compute(
    spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series, clock: MarketClock
) -> pd.DataFrame:
    """The derived label of `spec` at every decision time of `sigma` (module docstring)."""
    windows = label_windows(spec, quotes, sigma, clock)
    scale = horizon_scale(sigma, spec.horizon)
    derive = spec.params["derive"]
    ok = windows.ok.copy()
    if derive == "big":
        ok &= np.isfinite(scale)
    elif derive == "trade":
        ok &= np.isfinite(sigma.to_numpy(np.float64))
    value = np.full(len(ok), np.nan)
    if ok.any():
        bid = quotes["bid"].to_numpy(np.float64)
        ask = quotes["ask"].to_numpy(np.float64)
        e, x = windows.entry[ok], windows.exit[ok]
        r = side_return(spec.price_ref, bid, ask, e, x)
        if derive == "sign":
            value[ok] = np.sign(r)
        elif derive == "big":
            value[ok] = (np.abs(r) > float(spec.params["big_move_sigmas"]) * scale[ok]).astype(
                float
            )
        else:
            costs = round_trip_costs(
                spec, (bid[e] + ask[e]) / 2, sigma.to_numpy(np.float64)[ok],
                windows.ts[e], windows.ts[x], clock,
            )  # fmt: skip
            value[ok] = (r - costs > 0).astype(np.float64)
    return windows.frame(value, scale, clock, ok=ok)


def round_trip_costs(
    spec: TargetSpec,
    entry_mid: npt.NDArray[np.float64],
    sigma_rate_t: npt.NDArray[np.float64],
    start: npt.NDArray[np.int64],
    end: npt.NDArray[np.int64],
    clock: MarketClock,
) -> npt.NDArray[np.float64]:
    """Commission, slippage and financing of a round trip, as a log-return (module docstring)."""
    p = spec.params
    commission = 2 * float(p["commission_usd_per_oz_per_side"]) / entry_mid
    sigma_1m_bps = sigma_rate_t / _BPS  # sigma-hat per square-root minute = over one minute
    slippage = 2 * (
        float(p["slippage_fixed_bps"]) + float(p["slippage_sigma_multiple"]) * sigma_1m_bps
    )
    rate = (
        p["financing_long_annual_pct"]
        if spec.price_ref == "long"
        else p["financing_short_annual_pct"]
    )
    per_night = float(rate) / 100 / int(p["financing_day_count"])
    nights = financing_nights(clock, start, end, str(p["triple_weekday"]))
    out: npt.NDArray[np.float64] = commission + slippage * _BPS + nights * per_night
    return out


def financing_nights(
    clock: MarketClock, start: npt.NDArray[np.int64], end: npt.NDArray[np.int64], triple: str
) -> npt.NDArray[np.float64]:
    """Nights of financing between each start and end: one per market close c with
    ``start <= c < end`` (as `MarketClock.crosses_close`), three for a close on `triple`."""
    weekdays = pd.DatetimeIndex(pd.to_datetime(clock.closes, unit="ns", utc=True)).tz_convert(NY)
    weight = np.where(weekdays.weekday == WEEKDAYS.index(triple), 3.0, 1.0)
    cumulative = np.concatenate([[0.0], np.cumsum(weight)])
    before_end = np.searchsorted(clock.closes, end, side="left")
    before_start = np.searchsorted(clock.closes, start, side="left")
    nights: npt.NDArray[np.float64] = cumulative[before_end] - cumulative[before_start]
    return nights


def label_uniqueness(
    label_start: pd.Series | pd.DatetimeIndex,
    label_end: pd.Series | pd.DatetimeIndex,
    *,
    clock: MarketClock | None = None,
) -> pd.DataFrame:
    """Concurrency and average uniqueness of every label (module docstring).

    Args:
        label_start, label_end: Tz-aware label bounds, one row per label; NaT rows have no label.
        clock: Measure intervals in market time on this clock (wall time without one).

    Returns:
        Columns ``concurrency`` (the time-averaged number of labels occupying the interval),
        ``uniqueness`` (the time-average of 1 / concurrency) and ``weight_end`` (the latest
        ``label_end`` among the labels overlapping it), in the input's order and index; NaN and
        NaT for rows without a label.
    """
    index = label_start.index if isinstance(label_start, pd.Series) else None
    start_wall = ns_values(pd.DatetimeIndex(label_start))
    end_wall = ns_values(pd.DatetimeIndex(label_end))
    has = (start_wall != NAT_NS) & (end_wall != NAT_NS)
    if (end_wall[has] < start_wall[has]).any():
        raise ValueError("a label ends before it starts")
    concurrency = np.full(len(has), np.nan)
    uniqueness = np.full(len(has), np.nan)
    weight_end = np.full(len(has), NAT_NS, dtype=np.int64)
    if has.any():
        s_wall, e_wall = start_wall[has], end_wall[has]
        s, e = (clock.elapsed(s_wall), clock.elapsed(e_wall)) if clock else (s_wall, e_wall)
        c, u = _average_concurrency(s, e)
        concurrency[has], uniqueness[has] = c, u
        order = np.argsort(s_wall, kind="stable")
        latest = np.maximum.accumulate(e_wall[order])
        k = np.searchsorted(s_wall[order], e_wall, side="left")  # labels starting before the end
        overlapping = np.where(k > 0, latest[np.maximum(k - 1, 0)], e_wall)
        weight_end[has] = np.maximum(e_wall, overlapping)
    return pd.DataFrame(
        {
            "concurrency": concurrency,
            "uniqueness": uniqueness,
            "weight_end": pd.to_datetime(weight_end, unit="ns", utc=True),
        },
        index=index,
    )


def uniqueness_weights(uniqueness: pd.Series) -> pd.Series:
    """Average-uniqueness sample weights: `uniqueness` scaled to a mean of 1 over its labels."""
    known = uniqueness.notna()
    total = float(uniqueness[known].sum())
    if total <= 0:
        raise ValueError("no label with a positive uniqueness")
    weights: pd.Series = uniqueness * (int(known.sum()) / total)
    return weights.rename("weight")


def _average_concurrency(
    start: npt.NDArray[np.int64], end: npt.NDArray[np.int64]
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Time-averaged concurrency and 1 / concurrency of each ``[start, end)``.

    A label of zero length takes the concurrency at its instant, itself included.
    """
    bounds = np.unique(np.concatenate([start, end]))
    delta = np.zeros(len(bounds), dtype=np.int64)
    np.add.at(delta, np.searchsorted(bounds, start), 1)
    np.add.at(delta, np.searchsorted(bounds, end), -1)
    occupied = np.cumsum(delta)[:-1]  # labels occupying [bounds[j], bounds[j + 1])
    length = np.diff(bounds).astype(np.float64)
    safe = np.maximum(occupied, 1)
    inverse = np.concatenate([[0.0], np.cumsum(np.where(occupied > 0, length / safe, 0.0))])
    count = np.concatenate([[0.0], np.cumsum(length * occupied)])
    a, b = np.searchsorted(bounds, start), np.searchsorted(bounds, end)
    span = (end - start).astype(np.float64)
    positive = span > 0
    c = np.empty(len(start))
    u = np.empty(len(start))
    with np.errstate(invalid="ignore", divide="ignore"):
        c[positive] = (count[b] - count[a])[positive] / span[positive]
        u[positive] = (inverse[b] - inverse[a])[positive] / span[positive]
    if (~positive).any():
        at = a[~positive]
        inside = at < len(occupied)
        point = np.ones(len(at))  # the label itself
        point[inside] += occupied[at[inside]]
        c[~positive], u[~positive] = point, 1.0 / point
    return c, u


DERIVED_LABEL = TargetKind(
    name=KIND,
    code_version=1,
    expand=expand,
    sigma=sigma_rate,
    compute=compute,
    lookahead=lookahead,
)
