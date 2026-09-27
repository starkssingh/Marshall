"""Portfolio accounting of the event tier: one instrument, a USD account (BT-006).

The account books fills and financing and values the position at the latest mid:

- **Cash** starts at the capital and moves by realized price P&L, commissions and financing
  (a CFD account: notional is not paid for, only P&L settles).
- **FIFO lots.** Every fill first closes the oldest open lots of the opposite sign; whatever is
  left opens a new lot at the fill price. A closed (part of a) lot is a trade: its price P&L at
  the fill prices (spread and slippage are therefore inside it), its share of the commissions of
  the fills that opened and closed it, and its share of the financing charged while it was open
  (allocated over open lots by size at each rollover).
- **Unrealized P&L** is the open lots' ``lots x (mark - entry price) x contract``;
  **equity = cash + unrealized**.
- **Mark-to-market check.** Independently of the lots, equity is also the capital plus every
  fill's cash flow (``-lots x price x contract``), minus commissions and financing, plus the
  position's value at the mark. `Portfolio.equity_mark_to_market` computes it that way; the two
  agree at every step (tested after every event of engine runs).
- **Financing** at each rollover on the position held over it, at the mark (the mid of the last
  quote before the rollover), by `CostModel.financing_usd` (long or short rate, triple on the
  configured weekday).
- **Margin used** is ``|position| x contract x mark x margin_rate``.

`daily_frame` turns the engine's day-end snapshots, fills and financing into the screener's
daily layout (`xq.backtest.vectorized.DAILY_COLUMNS`) so both tiers share the BT-003 metrics:
``net_pnl`` is the change in equity, and ``gross_pnl = net_pnl + spread + slippage + commission
+ financing`` (the P&L with fills at the reference mids and no costs).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from xq.backtest.costs import CostModel
from xq.backtest.events import AccountState, Fill, Quote, clean_lots
from xq.backtest.vectorized import DAILY_COLUMNS
from xq.core.time import from_ns, trading_days

TRADE_COLUMNS = (
    "entry_time",
    "exit_time",
    "side",
    "lots",
    "entry_price",
    "exit_price",
    "price_pnl",
    "commission",
    "financing",
    "pnl",
    "open",
    "entry_fill_id",
    "exit_fill_id",
)


@dataclass
class OpenLot:
    """An open FIFO lot and the costs allocated to it so far."""

    fill_id: str
    opened_at: int
    lots: float
    price: float
    commission: float
    financing: float = 0.0


@dataclass(frozen=True)
class FinancingCharge:
    """One rollover charge."""

    ts: int
    multiplier: int
    lots: float
    mark: float
    amount: float


class Portfolio:
    """A one-instrument USD account (module docstring)."""

    def __init__(self, costs: CostModel, *, capital: float, margin_rate: float) -> None:
        if capital <= 0:
            raise ValueError("capital must be positive")
        self.costs = costs
        self.capital = float(capital)
        self.margin_rate = float(margin_rate)
        self.contract = float(costs.instrument.contract_size)
        self.cash = self.capital
        self.realized = 0.0
        self.commission = 0.0
        self.financing = 0.0
        self.spread_cost = 0.0
        self.slippage_cost = 0.0
        self.flows = 0.0
        self.mark_price = float("nan")
        self.lots: deque[OpenLot] = deque()
        self.trades: list[dict[str, Any]] = []
        self.charges: list[FinancingCharge] = []

    @property
    def position_lots(self) -> float:
        """The net position (signed lots)."""
        return clean_lots(sum(lot.lots for lot in self.lots))

    @property
    def unrealized(self) -> float:
        """Price P&L of the open lots at the mark (0 when flat)."""
        if not self.lots:
            return 0.0
        return sum(lot.lots * (self.mark_price - lot.price) for lot in self.lots) * self.contract

    @property
    def equity(self) -> float:
        """Cash plus unrealized P&L."""
        return self.cash + self.unrealized

    def equity_mark_to_market(self) -> float:
        """Equity from cash flows and the position's value, independent of the FIFO lots."""
        value = self.position_lots * self.mark_price * self.contract if self.lots else 0.0
        return self.capital + self.flows - self.commission - self.financing + value

    def mark(self, quote: Quote) -> None:
        """Value the position at `quote`'s mid from now on."""
        self.mark_price = quote.mid

    def state(self, ts: int) -> AccountState:
        """The account at `ts`."""
        position = self.position_lots
        margin = (
            abs(position) * self.contract * self.mark_price * self.margin_rate if position else 0.0
        )
        return AccountState(
            ts=ts,
            capital=self.capital,
            cash=self.cash,
            unrealized=self.unrealized,
            equity=self.equity,
            position_lots=position,
            margin_used=margin,
            mark=self.mark_price,
        )

    def book(self, fill: Fill) -> None:
        """Book a fill: cash flows, costs and FIFO lots (module docstring)."""
        size = abs(fill.lots)
        if size == 0:
            raise ValueError("a fill must trade a non-zero size")
        self.flows -= fill.lots * fill.price * self.contract
        self.commission += fill.commission
        self.spread_cost += fill.spread_cost
        self.slippage_cost += fill.slippage_cost
        self.cash -= fill.commission
        per_lot = fill.commission / size
        remaining = fill.lots
        while remaining != 0 and self.lots and np.sign(self.lots[0].lots) != np.sign(remaining):
            lot = self.lots[0]
            closed = min(abs(lot.lots), abs(remaining))
            share = closed / abs(lot.lots)
            side = float(np.sign(lot.lots))
            price_pnl = side * closed * (fill.price - lot.price) * self.contract
            commission = lot.commission * share + per_lot * closed
            financing = lot.financing * share
            self.realized += price_pnl
            self.cash += price_pnl
            self.trades.append(
                {
                    "entry_time": from_ns(lot.opened_at),
                    "exit_time": from_ns(fill.ts),
                    "side": side,
                    "lots": closed,
                    "entry_price": lot.price,
                    "exit_price": fill.price,
                    "price_pnl": price_pnl,
                    "commission": commission,
                    "financing": financing,
                    "pnl": price_pnl - commission - financing,
                    "open": False,
                    "entry_fill_id": lot.fill_id,
                    "exit_fill_id": fill.fill_id,
                }
            )
            lot.commission -= lot.commission * share
            lot.financing -= financing
            lot.lots = clean_lots(lot.lots - side * closed)
            remaining = clean_lots(remaining + side * closed)
            if lot.lots == 0:
                self.lots.popleft()
        if remaining != 0:
            self.lots.append(
                OpenLot(fill.fill_id, fill.ts, remaining, fill.price, per_lot * abs(remaining))
            )

    def charge_financing(self, ts: int, multiplier: int) -> float:
        """Charge financing on the position held over the rollover at `ts`; return the charge."""
        position = self.position_lots
        if position == 0:
            return 0.0
        if np.isnan(self.mark_price):
            raise ValueError("a position without a mark cannot be financed")
        amount = float(self.costs.financing_usd([position], [self.mark_price], [multiplier])[0])
        self.cash -= amount
        self.financing += amount
        for lot in self.lots:
            lot.financing += amount * abs(lot.lots) / abs(position)
        self.charges.append(FinancingCharge(ts, multiplier, position, self.mark_price, amount))
        return amount

    def trades_frame(self) -> pd.DataFrame:
        """Closed FIFO trades, then the open lots marked at the mark (``open`` True)."""
        records = list(self.trades)
        for lot in self.lots:
            side = float(np.sign(lot.lots))
            price_pnl = lot.lots * (self.mark_price - lot.price) * self.contract
            records.append(
                {
                    "entry_time": from_ns(lot.opened_at),
                    "exit_time": pd.NaT,
                    "side": side,
                    "lots": abs(lot.lots),
                    "entry_price": lot.price,
                    "exit_price": self.mark_price,
                    "price_pnl": price_pnl,
                    "commission": lot.commission,
                    "financing": lot.financing,
                    "pnl": price_pnl - lot.commission - lot.financing,
                    "open": True,
                    "entry_fill_id": lot.fill_id,
                    "exit_fill_id": None,
                }
            )
        return pd.DataFrame(records, columns=list(TRADE_COLUMNS))

    def financing_series(self) -> pd.Series:
        """The rollover charges, indexed by rollover instant."""
        index = pd.DatetimeIndex([from_ns(c.ts) for c in self.charges], name="rollover")
        if not len(index):
            index = pd.DatetimeIndex([], tz="UTC", name="rollover")
        return pd.Series(
            [c.amount for c in self.charges], index=index, name="financing", dtype="float64"
        )


def fills_frame(fills: list[Fill]) -> pd.DataFrame:
    """Fills as a frame, with the screener's column names where they mean the same."""
    columns = (
        "fill_id",
        "order_id",
        "decision_id",
        "intent_id",
        "decision_time",
        "fill_time",
        "role",
        "order_type",
        "lots",
        "position_lots",
        "bid",
        "ask",
        "mid",
        "slippage_bps",
        "price",
        "spread_cost",
        "slippage_cost",
        "commission",
        "bar_start",
    )
    rows = [
        {
            "fill_id": f.fill_id,
            "order_id": f.order_id,
            "decision_id": f.decision_id,
            "intent_id": f.intent_id,
            "decision_time": from_ns(f.decided_at),
            "fill_time": from_ns(f.ts),
            "role": f.role,
            "order_type": f.order_type,
            "lots": f.lots,
            "position_lots": f.position_after,
            "bid": f.bid,
            "ask": f.ask,
            "mid": f.mid,
            "slippage_bps": f.slippage_bps,
            "price": f.price,
            "spread_cost": f.spread_cost,
            "slippage_cost": f.slippage_cost,
            "commission": f.commission,
            "bar_start": pd.NaT if f.bar_start is None else from_ns(f.bar_start),
        }
        for f in fills
    ]
    frame = pd.DataFrame(rows, columns=list(columns))
    for column in ("decision_time", "fill_time", "bar_start"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    return frame


def daily_frame(
    days: list[tuple[date, AccountState]],
    fills: pd.DataFrame,
    financing: pd.Series,
    *,
    capital: float,
    contract: float,
) -> pd.DataFrame:
    """Daily P&L in the screener's layout from day-end snapshots (module docstring).

    Args:
        days: (trading day, account state at its end) per trading day, in order.
        fills: `fills_frame` output.
        financing: Rollover charges indexed by instant; a charge at a day's end belongs to it.
        capital: The account's starting capital.
        contract: Contract size (units per lot).
    """
    if not days:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in DAILY_COLUMNS})
    labels = [day for day, _ in days]
    states = [state for _, state in days]
    equity = np.array([s.equity for s in states], dtype=np.float64)
    net = np.diff(np.concatenate([[capital], equity]))

    def per_day(
        times: pd.Series | pd.DatetimeIndex, values: np.ndarray, *, at_end: bool
    ) -> np.ndarray:
        stamps = pd.DatetimeIndex(times)
        if at_end:
            stamps = stamps - pd.Timedelta(1, "ns")
        keys = [d.item() for d in trading_days(stamps)] if len(stamps) else []
        totals = pd.Series(values, dtype="float64").groupby(pd.Index(keys, dtype=object)).sum()
        return totals.reindex(labels, fill_value=0.0).to_numpy(np.float64)

    spread = per_day(fills["fill_time"], fills["spread_cost"].to_numpy(np.float64), at_end=False)
    slippage = per_day(
        fills["fill_time"], fills["slippage_cost"].to_numpy(np.float64), at_end=False
    )
    commission = per_day(fills["fill_time"], fills["commission"].to_numpy(np.float64), at_end=False)
    charges = per_day(
        pd.DatetimeIndex(financing.index), financing.to_numpy(np.float64), at_end=True
    )
    lots = np.array([s.position_lots for s in states], dtype=np.float64)
    marks = np.array([0.0 if s.position_lots == 0 else s.mark for s in states], dtype=np.float64)
    return pd.DataFrame(
        {
            "gross_pnl": net + spread + slippage + commission + charges,
            "spread_cost": spread,
            "slippage_cost": slippage,
            "commission": commission,
            "financing": charges,
            "net_pnl": net,
            "equity": equity,
            "return": net / capital,
            "position_lots": lots,
            "exposure": np.abs(lots * marks * contract) / capital,
        },
        index=pd.Index(labels, name="trading_day"),
    )
