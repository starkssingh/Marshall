"""Derived labels, label concurrency and average-uniqueness sample weights (TGT-006).

**Derived labels** (kind ``derived_label``) come from the forward returns of TGT-002 (the same
windows: entry and exit fills after the latency, market time, ``label_end`` at the exit fill):

- ``tgt_sign_<h>``: the sign of the mid return (-1, 0 or +1);
- ``tgt_big_<h>``: 1 when ``|mid return| > big_move_sigmas * s``, else 0, where
  ``s = sigma_t * sqrt(h in market minutes)`` is the interim sigma-hat known at t over the horizon;
- ``tgt_trade_<ref>_<h>`` (``long``, ``short``): 1 when a one-lot round trip on that side, entered
  at the window's entry fill and left at its exit fill, makes money net of every cost, else 0 —
  the trade/no-trade label. The costs are **the backtester's own** `CostModel` (the target set's
  ``cost_model``, bound by `xq.targets.kinds.target_specs`; C-30 (3), ADR 0065), applied as the
  vectorized screener applies them: the fill prices are the ask and the bid moved by the model's
  slippage (`CostModel.slippage_bps`, its session and event multipliers included — three times in
  the rollover window, twice around US releases — on the sigma-hat known at t over one market
  minute), the commission is `CostModel.commission_usd` of each fill at its mid, and financing is
  `CostModel.financing_usd` at every rollover after the entry fill up to the exit fill
  (`CostModel.rollovers`, three times on the triple weekday), marked at the mid of the last quote
  before it. "Expected net P&L" is read as the trade's realized net P&L with the model's costs.

The price references choose the labels: ``mid`` gives the sign and big-move labels, ``long`` and
``short`` the trade labels.

**Concurrency and uniqueness** (`label_uniqueness`). Each label occupies ``[label_start,
label_end)``, measured in market time when a `MarketClock` is given (closed periods do not count).
The concurrency at an instant is the number of labels occupying it. A label's **average
uniqueness** is the time-average of ``1 / concurrency`` over its interval: 1 for a label that
overlaps no other, ``1/k`` for k identical labels. Average-uniqueness **sample weights**
(`uniqueness_weights`) scale it to a mean of 1 over the labels passed, so they are computed within
a training set, never across folds. A label's uniqueness depends on every label overlapping it,
so it is known only at ``weight_end``, the latest ``label_end`` among them. When weights are
computed over labels that reach into a later window, the splitters purge by
``max(label_end, weight_end)`` (their ``weight_end`` argument; C-30 (4), ADR 0065).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import Field

from xq.core.config import TargetSetConfig
from xq.core.errors import ConfigError
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

if TYPE_CHECKING:
    from xq.backtest.costs import CostModel

KIND = "derived_label"
_BPS = 1e-4
_MINUTE = pd.Timedelta(minutes=1)


class DerivedLabelParams(ExecutionParams):
    """Parameters of a ``derived_label`` target set (module docstring).

    ``cost_model`` names the backtester's cost model (``config/costs/<name>.yaml``) the trade
    labels are priced with; its latency and fill delay must equal the execution parameters.
    """

    big_move_sigmas: float = Field(gt=0)
    cost_model: str = Field(min_length=1)


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


def cost_model(definition: TargetSetConfig) -> str:
    """The cost model the trade labels are priced with."""
    return derived_label_params(definition.params).cost_model


def lookahead(definition: TargetSetConfig, trading_day: pd.Timedelta) -> Lookahead:
    """The longest horizon plus latency in market time, then the allowed fill delay."""
    return execution_lookahead(derived_label_params(definition.params), definition, trading_day)


def compute(
    spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series, clock: MarketClock
) -> pd.DataFrame:
    """The derived label of `spec` at every decision time of `sigma` (module docstring).

    Raises:
        ConfigError: for a trade label whose spec carries no cost model (see `target_specs`).
    """
    windows = label_windows(spec, quotes, sigma, clock)
    scale = horizon_scale(sigma, spec.horizon)
    derive = spec.params["derive"]
    ok = windows.ok.copy()
    if derive == "big":
        ok &= np.isfinite(scale)
    elif derive == "trade":
        if spec.costs is None:
            raise ConfigError(
                f"trade label {spec.name} needs the backtester's cost model: build its specs with "
                "xq.targets.kinds.target_specs"
            )
        ok &= np.isfinite(sigma.to_numpy(np.float64))
    value = np.full(len(ok), np.nan)
    if ok.any():
        bid = quotes["bid"].to_numpy(np.float64)
        ask = quotes["ask"].to_numpy(np.float64)
        e, x = windows.entry[ok], windows.exit[ok]
        if derive == "sign":
            value[ok] = np.sign(side_return(spec.price_ref, bid, ask, e, x))
        elif derive == "big":
            r = side_return(spec.price_ref, bid, ask, e, x)
            value[ok] = (np.abs(r) > float(spec.params["big_move_sigmas"]) * scale[ok]).astype(
                float
            )
        else:
            assert spec.costs is not None  # checked above
            sigma_1m_bps = sigma.to_numpy(np.float64)[ok] / _BPS
            pnl = round_trip_pnl(
                spec.costs, spec.price_ref, windows.ts, bid, ask, e, x, sigma_1m_bps
            )
            value[ok] = (pnl["net_usd"].to_numpy(np.float64) > 0).astype(np.float64)
    return windows.frame(value, scale, clock, ok=ok)


def round_trip_pnl(
    costs: CostModel,
    side: str,
    ts: npt.NDArray[np.int64],
    bid: npt.NDArray[np.float64],
    ask: npt.NDArray[np.float64],
    entry: npt.NDArray[np.int64],
    exit_: npt.NDArray[np.int64],
    sigma_1m_bps: npt.NDArray[np.float64],
) -> pd.DataFrame:
    """P&L in USD of one-lot round trips on `side` (``long`` or ``short``), entered at quote rows
    `entry` and left at rows `exit_` of (`ts`, `bid`, `ask`), with the costs of `costs` applied
    as the vectorized screener applies them (module docstring).

    Returns:
        One row per round trip: ``entry_slippage_bps`` and ``exit_slippage_bps`` (the model's,
        multipliers included), ``gross_usd`` (at the quotes' ask and bid, spread paid),
        ``slippage_usd``, ``commission_usd``, ``financing_usd`` and ``net_usd``.
    """
    contract = float(costs.instrument.contract_size)
    times = pd.DatetimeIndex(pd.to_datetime(np.concatenate([ts[entry], ts[exit_]]), utc=True))
    slip = costs.slippage_bps(times, np.concatenate([sigma_1m_bps, sigma_1m_bps]))
    slip_in, slip_out = slip[: len(entry)], slip[len(entry) :]
    long = side == "long"
    paid_in = np.where(long, ask[entry], bid[entry])  # buy at the ask, sell at the bid
    paid_out = np.where(long, bid[exit_], ask[exit_])
    direction = 1.0 if long else -1.0
    gross = direction * contract * (paid_out - paid_in)
    slippage = contract * _BPS * (paid_in * slip_in + paid_out * slip_out)
    one = np.ones(len(entry))
    commission = costs.commission_usd(one, (bid[entry] + ask[entry]) / 2) + costs.commission_usd(
        one, (bid[exit_] + ask[exit_]) / 2
    )
    financing = np.zeros(len(entry))
    if len(entry):
        start = pd.Timestamp(int(ts[entry].min()), tz="UTC")
        end = pd.Timestamp(int(ts[exit_].max()), tz="UTC") + pd.Timedelta(1, "ns")
        rolls = costs.rollovers(start, end)
        r = ns_values(pd.DatetimeIndex(rolls.index))
        if len(r):
            # held over a rollover after its entry fill, up to and including its exit fill (the
            # screener's position after the last fill strictly before the rollover)
            last = np.searchsorted(ts, r, side="left") - 1
            mark = (bid[last] + ask[last]) / 2
            charge = costs.financing_usd(direction * np.ones(len(r)), mark, rolls.to_numpy())
            cumulative = np.concatenate([[0.0], np.cumsum(charge)])
            after_entry = np.searchsorted(r, ts[entry], side="right")
            through_exit = np.searchsorted(r, ts[exit_], side="right")
            financing = cumulative[through_exit] - cumulative[after_entry]
    net = gross - slippage - commission - financing
    return pd.DataFrame(
        {
            "entry_slippage_bps": slip_in,
            "exit_slippage_bps": slip_out,
            "gross_usd": gross,
            "slippage_usd": slippage,
            "commission_usd": commission,
            "financing_usd": financing,
            "net_usd": net,
        }
    )


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
    # 2: trade labels priced by the backtester's cost model, multipliers included (C-30 (3))
    code_version=2,
    expand=expand,
    sigma=sigma_rate,
    compute=compute,
    lookahead=lookahead,
    cost_model=cost_model,
)
