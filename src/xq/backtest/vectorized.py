"""Vectorized screener with next-quote fills (BT-002).

`run_vectorized(positions, quotes, costs, clock, capital=...)` turns a series of target exposures
into fills, daily P&L and trades:

- **Positions** are target exposures decided at decision times: ``+1`` is long a notional equal to
  `capital`, ``-0.5`` short half of it. When the target changes, the trade is sized in lots at the
  mid price of its fill; while the target is unchanged, the lots are unchanged (no rebalancing).
  This is a research screener: the event-driven tier (BT-004+) routes orders through the risk
  engine, which sizes them.
- **Fills** happen at the first quote at or after ``latency`` of *market time* after the decision,
  on the correct side — buy at the ask, sell at the bid — plus slippage. Never at the signal bar's
  close, never at mid. If that quote comes more than ``max_fill_delay`` after the intended time,
  the trade is missed and the position stays; the next decision tries again from the position
  actually held.
- **Decisions taken while the market is closed** (the 17:00 close itself, the daily break,
  weekends, holidays) place no order at all — no entry, no exit, no change (ADR 0032). The
  position held stays until the next decision taken while the market is open; the skipped
  decisions that would have traded are reported in ``closed``.
- **Costs**: the half-spread against mid is paid by each fill; slippage and commission come from
  the cost model; financing is charged at every rollover on the lots held over it.
- **Days** are trading days (17:00 New York roll) with quotes. Positions are marked at the mid of
  the last quote before each day's end. ``net_pnl`` is the change in equity; ``gross_pnl`` is
  what it would have been with fills at mid and no costs, so ``net = gross - spread - slippage -
  commission - financing`` holds exactly. Daily ``return`` is ``net_pnl / capital``.
- **Trades** are holding episodes: from leaving flat (or flipping side) to returning to flat (or
  flipping), with their net P&L including costs and financing. An episode still open at the end is
  marked at the last mid and flagged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.backtest.costs import CostModel
from xq.core.errors import NaiveTimestampError
from xq.core.time import trading_day_bounds, trading_days
from xq.data.calendar import NAT_NS, MarketClock

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
_BPS = 1e-4
DAILY_COLUMNS = (
    "gross_pnl",
    "spread_cost",
    "slippage_cost",
    "commission",
    "financing",
    "net_pnl",
    "equity",
    "return",
    "position_lots",
    "exposure",
)


@dataclass(frozen=True)
class BacktestResult:
    """Fills, skipped decisions, daily P&L and trades of one screened position series.

    `missed`: decisions whose fill would have come too late; `closed`: decisions taken while the
    market was closed, which place no order.
    """

    fills: pd.DataFrame
    missed: pd.DatetimeIndex
    closed: pd.DatetimeIndex
    daily: pd.DataFrame
    trades: pd.DataFrame
    financing: pd.Series
    capital: float
    contract_size: float


def run_vectorized(
    positions: pd.Series,
    quotes: pd.DataFrame,
    costs: CostModel,
    clock: MarketClock,
    *,
    capital: float,
    sigma_1m_bps: pd.Series | None = None,
) -> BacktestResult:
    """Screen `positions` against `quotes` (see the module docstring).

    Args:
        positions: Target exposure per decision time (tz-aware, unique, increasing; no NaN).
        quotes: Usable quotes with ``ts_utc`` (tz-aware), ``bid`` and ``ask``, in time order.
        costs: The cost model.
        clock: Market clock covering the decision times (for the latency).
        capital: Capital the exposures refer to (USD).
        sigma_1m_bps: Sigma-hat of 1-minute returns (bps) known at each decision time, for
            slippage; zero when omitted.
    """
    decisions = _checked_positions(positions)
    t = _ns(decisions)
    target = positions.to_numpy(dtype=np.float64)
    ts = _ns(pd.DatetimeIndex(quotes["ts_utc"])) if len(quotes) else np.array([], np.int64)
    if len(ts) > 1 and np.any(np.diff(ts) < 0):
        raise ValueError("quotes must be in time order")
    bid = quotes["bid"].to_numpy(dtype=np.float64)
    ask = quotes["ask"].to_numpy(dtype=np.float64)
    sigma = (
        np.zeros(len(t))
        if sigma_1m_bps is None
        else sigma_1m_bps.reindex(decisions).fillna(0.0).to_numpy(np.float64)
    )
    contract = float(costs.instrument.contract_size)

    market_open = clock.is_open(t) if len(t) else np.array([], dtype=bool)
    intended = clock.advance(t, costs.latency.value) if len(t) else np.array([], np.int64)
    quote = np.searchsorted(ts, intended, side="left")
    found = (quote < len(ts)) & (intended != NAT_NS)
    timely = np.zeros(len(t), dtype=bool)
    timely[found] = ts[quote[found]] - intended[found] <= costs.max_fill_delay.value

    rows: list[tuple[int, int, float, float]] = []  # decision, quote, trade lots, lots after
    missed: list[int] = []
    closed: list[int] = []
    held_exposure, lots = 0.0, 0.0
    for i in range(len(t)):
        if target[i] == held_exposure:
            continue
        if not market_open[i]:
            closed.append(i)
            continue
        if not timely[i]:
            missed.append(i)
            continue
        q = int(quote[i])
        mid = (bid[q] + ask[q]) / 2
        new_lots = target[i] * capital / (mid * contract)
        rows.append((i, q, new_lots - lots, new_lots))
        held_exposure, lots = target[i], new_lots

    fills = _fills(rows, decisions, target, ts, bid, ask, sigma, costs, contract)
    financing = _financing(fills, ts, bid, ask, costs)
    daily = _daily(fills, financing, ts, bid, ask, capital, contract)
    trades = _trades(fills, financing, bid, ask, contract)
    return BacktestResult(
        fills, decisions[missed], decisions[closed], daily, trades, financing, capital, contract
    )


def _fills(
    rows: list[tuple[int, int, float, float]],
    decisions: pd.DatetimeIndex,
    target: FloatArray,
    ts: IntArray,
    bid: FloatArray,
    ask: FloatArray,
    sigma: FloatArray,
    costs: CostModel,
    contract: float,
) -> pd.DataFrame:
    i = np.array([r[0] for r in rows], dtype=np.int64)
    q = np.array([r[1] for r in rows], dtype=np.int64)
    lots = np.array([r[2] for r in rows], dtype=np.float64)
    after = np.array([r[3] for r in rows], dtype=np.float64)
    fill_time = pd.DatetimeIndex(pd.to_datetime(ts[q], unit="ns", utc=True))
    b, a = bid[q], ask[q]
    mid = (b + a) / 2
    slip = costs.slippage_bps(fill_time, sigma[i])
    buy = lots > 0
    side_price = np.where(buy, a, b)
    size = np.abs(lots)
    return pd.DataFrame(
        {
            "decision_time": decisions[i],
            "fill_time": fill_time,
            "target": target[i],
            "lots": lots,
            "position_lots": after,
            "bid": b,
            "ask": a,
            "mid": mid,
            "slippage_bps": slip,
            "price": np.where(buy, a * (1 + slip * _BPS), b * (1 - slip * _BPS)),
            "spread_cost": size * (a - b) / 2 * contract,
            "slippage_cost": size * side_price * slip * _BPS * contract,
            "commission": costs.commission_usd(lots, mid),
        }
    )


def _financing(
    fills: pd.DataFrame, ts: IntArray, bid: FloatArray, ask: FloatArray, costs: CostModel
) -> pd.Series:
    """Charges at rollovers from the first fill to the last quote, on the lots held over each."""
    if fills.empty or len(ts) == 0:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC", name="rollover"))
    start = pd.Timestamp(fills["fill_time"].iloc[0])
    end = pd.Timestamp(int(ts[-1]), tz="UTC") + pd.Timedelta(1, "ns")
    rolls = costs.rollovers(start, end)
    r = _ns(pd.DatetimeIndex(rolls.index))
    held = _held_at(fills, r)
    last = np.maximum(np.searchsorted(ts, r, side="left") - 1, 0)
    charge = costs.financing_usd(held, (bid[last] + ask[last]) / 2, rolls.to_numpy())
    return pd.Series(charge, index=rolls.index, name="financing")


def _held_at(fills: pd.DataFrame, instants: IntArray) -> FloatArray:
    """Lots held at each instant: the position after the last fill strictly before it."""
    fill_ns = _ns(pd.DatetimeIndex(fills["fill_time"]))
    k = np.searchsorted(fill_ns, instants, side="left") - 1
    after = fills["position_lots"].to_numpy(np.float64)
    held: FloatArray = np.zeros(len(instants))
    held[k >= 0] = after[k[k >= 0]]
    return held


def _daily(
    fills: pd.DataFrame,
    financing: pd.Series,
    ts: IntArray,
    bid: FloatArray,
    ask: FloatArray,
    capital: float,
    contract: float,
) -> pd.DataFrame:
    if len(ts) == 0:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in DAILY_COLUMNS})
    days = np.unique(trading_days(pd.DatetimeIndex(pd.to_datetime(ts, unit="ns", utc=True))))
    if not fills.empty:
        days = days[days >= trading_days(pd.DatetimeIndex(fills["fill_time"][:1]))[0]]
    ends = np.array([trading_day_bounds(d.item())[1].value for d in days], dtype=np.int64)
    last = np.searchsorted(ts, ends, side="left") - 1
    mark = (bid[last] + ask[last]) / 2
    lots = _held_at(fills, ends)
    fill_ns = _ns(pd.DatetimeIndex(fills["fill_time"]))
    fin_ns = _ns(pd.DatetimeIndex(financing.index))

    def to_day_end(times: IntArray, values: FloatArray, *, at_end: bool = False) -> FloatArray:
        """Cumulative sum of `values` at each day end: events before it, or at it with `at_end`.

        A rollover happens exactly at the day end (17:00 New York) and belongs to the day it
        closes; a fill at that instant belongs to the next day.
        """
        total = np.concatenate([[0.0], np.cumsum(values)])
        side: Literal["left", "right"] = "right" if at_end else "left"
        result: FloatArray = total[np.searchsorted(times, ends, side=side)]
        return result

    def per_day(times: IntArray, values: FloatArray, *, at_end: bool = False) -> FloatArray:
        cumulative = to_day_end(times, values, at_end=at_end)
        result: FloatArray = np.diff(np.concatenate([[0.0], cumulative]))
        return result

    traded = fills["lots"].to_numpy(np.float64) * contract
    position_value = lots * mark * contract
    commission = fills["commission"].to_numpy(np.float64)
    charges = financing.to_numpy(np.float64)
    equity = (
        capital
        + to_day_end(fill_ns, -traded * fills["price"].to_numpy(np.float64))
        - to_day_end(fill_ns, commission)
        - to_day_end(fin_ns, charges, at_end=True)
        + position_value
    )
    gross_equity = capital + to_day_end(fill_ns, -traded * fills["mid"].to_numpy(np.float64))
    gross_equity = gross_equity + position_value
    net = np.diff(np.concatenate([[capital], equity]))
    return pd.DataFrame(
        {
            "gross_pnl": np.diff(np.concatenate([[capital], gross_equity])),
            "spread_cost": per_day(fill_ns, fills["spread_cost"].to_numpy(np.float64)),
            "slippage_cost": per_day(fill_ns, fills["slippage_cost"].to_numpy(np.float64)),
            "commission": per_day(fill_ns, commission),
            "financing": per_day(fin_ns, charges, at_end=True),
            "net_pnl": net,
            "equity": equity,
            "return": net / capital,
            "position_lots": lots,
            "exposure": np.abs(position_value) / capital,
        },
        index=pd.Index([d.item() for d in days], name="trading_day"),
    )


@dataclass
class _Episode:
    entry_time: pd.Timestamp
    side: float
    max_lots: float = 0.0
    pnl: float = 0.0
    exit_time: pd.Timestamp | None = None


def _trades(
    fills: pd.DataFrame,
    financing: pd.Series,
    bid: FloatArray,
    ask: FloatArray,
    contract: float,
) -> pd.DataFrame:
    """Holding episodes with their net P&L (see the module docstring)."""
    done: list[_Episode] = []
    current: _Episode | None = None
    per_fill = zip(
        fills["lots"].to_numpy(np.float64),
        fills["position_lots"].to_numpy(np.float64),
        fills["price"].to_numpy(np.float64),
        fills["commission"].to_numpy(np.float64),
        pd.DatetimeIndex(fills["fill_time"]),
        strict=True,
    )
    for lots, after, price, commission, when in per_fill:
        before = after - lots
        flow = -lots * price * contract - commission
        if current is not None and (after == 0 or np.sign(after) != np.sign(before)):
            share = abs(before / lots)  # the part of this fill that closes the episode
            current.pnl += share * flow
            current.exit_time = when
            done.append(current)
            current = None
            flow *= 1 - share
        if after != 0:
            if current is None:
                current = _Episode(when, float(np.sign(after)))
            current.pnl += flow
            current.max_lots = max(current.max_lots, abs(after))
    still_open = current is not None
    if current is not None and len(bid):
        current.pnl += float(fills["position_lots"].iloc[-1]) * (bid[-1] + ask[-1]) / 2 * contract
        done.append(current)
    times = pd.DatetimeIndex(financing.index)
    charges = financing.to_numpy(np.float64)
    records = []
    for n, episode in enumerate(done):
        is_open = still_open and n == len(done) - 1
        inside = times > episode.entry_time
        if episode.exit_time is not None:
            inside &= times <= episode.exit_time
        records.append(
            {
                "entry_time": episode.entry_time,
                "exit_time": episode.exit_time if not is_open else pd.NaT,
                "side": episode.side,
                "max_lots": episode.max_lots,
                "pnl": episode.pnl - float(charges[inside].sum()),
                "open": is_open,
            }
        )
    columns = ["entry_time", "exit_time", "side", "max_lots", "pnl", "open"]
    return pd.DataFrame(records, columns=columns)


def _checked_positions(positions: pd.Series) -> pd.DatetimeIndex:
    index = positions.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise NaiveTimestampError("positions must be indexed by tz-aware decision times")
    if not index.is_unique or not index.is_monotonic_increasing:
        raise ValueError("decision times must be unique and increasing")
    if positions.isna().any():
        raise ValueError("positions must not be missing; use 0 for flat")
    return index


def _ns(index: pd.DatetimeIndex) -> IntArray:
    values: IntArray = (
        index.tz_convert("UTC").as_unit("ns").to_numpy(dtype="datetime64[ns]").view(np.int64)
    )
    return values
