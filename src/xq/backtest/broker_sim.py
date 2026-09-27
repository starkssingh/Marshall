"""Broker simulator: orders, brackets, gaps and intrabar resolution (BT-005).

The simulator is the event tier's execution venue. It holds the net position, the working orders
and the brackets, and turns market data into fills. The rules:

- **Arrival.** An order sent at t arrives ``latency`` of *market time* later (`MarketClock.advance`,
  as BT-002); one that would arrive beyond the market clock's range never arrives (expired). On
  arrival every working order of an earlier intent is cancelled ("replaced"), then the order is
  rejected if the position is no longer the one its decision saw, or if it opens or increases
  exposure and the margin it needs (``|position after| x contract x mid x margin_rate``) exceeds
  the equity, or — for an entry — if it arrives inside an entry blackout (BT-008).
- **Market orders** fill at the first quote at or after arrival **on the correct side** — buy at
  the ask, sell at the bid — plus slippage from the cost model (sigma-hat known at the decision).
  If that quote comes more than ``max_fill_delay`` after arrival the order expires (the engine's
  timer fires at ``arrival + max_fill_delay + 1 ns``). Never at mid, never at the signal bar's
  close.
- **Stop orders** trigger when the bid (sell stop) falls to or below, or the ask (buy stop) rises
  to or above, the stop price, and fill at **that first quote beyond the stop** plus slippage: a
  gap through the stop fills at the gapped price, not at the stop.
- **Limit orders** trigger when the bid (sell limit) reaches or exceeds, or the ask (buy limit)
  reaches or falls below, the limit, and fill at the limit price: never better (no price
  improvement, the pessimistic choice) and without slippage. Their reference quote is the limit
  on the order's side with the triggering quote's spread, so the fill pays the half-spread and
  nothing else.
- **Brackets.** An order with a stop and/or target gets an OCO bracket on the whole resulting
  position once it fills: a stop order (stop loss) and a limit order (take profit) on the closing
  side, active from the next quote. When one leg fills the other is cancelled.
- **Closed market.** No order fills on a quote while the market is closed; working stops and limits
  wait for the reopen and then fill at the first quote beyond their level (a weekend gap fills at
  the gapped price).
- **Bar mode** (one-minute bid/ask bars, no ticks). At a bar's open: market orders that arrived at
  or before the bar's start fill at the open; stops and limits the open has already passed fill
  at the open (gap). Over the bar's range: a stop touched by the bar's low (sell) or high (buy)
  fills at the stop price plus slippage; a limit touched fills at the limit. **If both legs of a
  bracket are touched in the same bar, the stop loss is assumed to have come first** (pessimistic)
  and the bar is counted as ambiguous; with ticks the quotes decide. Fills over a range are stamped
  just before the bar's end and carry the bar's start.

Every fill carries its cost decomposition against the reference mid (`xq.backtest.events.Fill`):
the half-spread, the slippage and fill-rule cost, and the commission (`CostModel.commission_usd`
at the mid). Rejections, cancellations, expiries and bracket orders are written to the recorder,
each linked to the order and the risk decision it belongs to.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np
import pandas as pd

from xq.backtest.costs import CostModel
from xq.backtest.events import (
    ExecutionBar,
    ExecutionBarEvent,
    Fill,
    TickEvent,
    clean_lots,
)
from xq.core.time import ensure_utc_index
from xq.core.types import Side
from xq.data.calendar import NAT_NS, MarketClock
from xq.signals.schema import OrderIntent

OrderType = Literal["market", "limit", "stop"]
Role = Literal["entry", "exit", "stop_loss", "take_profit"]
Status = Literal["sent", "working", "filled", "cancelled", "rejected", "expired"]
_BPS = 1e-4


class BrokerRecorder(Protocol):
    """The part of the decision ledger the broker writes (BT-007)."""

    def fill(self, fill: Fill) -> None:
        """An execution, recorded when it happens (before the brackets or cancels it causes)."""
        ...

    def order_rejected(self, order_id: str, ts: int, reason: str) -> None:
        """An order was refused on arrival."""
        ...

    def order_cancelled(self, order_id: str, ts: int, reason: str) -> None:
        """A working order was cancelled."""
        ...

    def order_expired(self, order_id: str, ts: int, reason: str) -> None:
        """An order expired unfilled."""
        ...

    def bracket_order(
        self,
        *,
        order_id: str,
        parent_order_id: str,
        decision_id: str,
        intent_id: str,
        ts: int,
        side: Side,
        lots: float,
        order_type: OrderType,
        price: float,
        role: Role,
    ) -> None:
        """A bracket leg was placed after its parent order filled."""
        ...


@dataclass
class WorkingOrder:
    """An order at the broker and its state."""

    order_id: str
    decision_id: str
    intent_id: str
    decided_at: int
    side: Side
    lots: float
    order_type: OrderType
    price: float | None
    role: Role
    status: Status
    expected_position: float | None = None
    stop: float | None = None
    target: float | None = None
    parent_order_id: str | None = None
    arrival: int | None = None
    expires_at: int | None = None
    active_from: int = 0

    @property
    def is_bracket(self) -> bool:
        """True for a stop-loss or take-profit leg."""
        return self.parent_order_id is not None


@dataclass
class BracketRecord:
    """One OCO bracket: its levels, when it was active and how it ended."""

    parent_order_id: str
    side: Side  # the side of the position it protects
    stop: float | None
    target: float | None
    activated_at: int
    closed_at: int | None = None
    exit_role: Role | None = None
    ambiguous: bool = False


class SimulatedBroker:
    """The event tier's execution venue (module docstring)."""

    def __init__(
        self,
        costs: CostModel,
        clock: MarketClock,
        *,
        recorder: BrokerRecorder,
        margin_rate: float,
        equity: Callable[[int], float],
        entry_blackout: Callable[[int], str | None] | None = None,
        sigma_1m_bps: pd.Series | None = None,
    ) -> None:
        if not 0 < margin_rate <= 1:
            raise ValueError("margin_rate must be in (0, 1]")
        self.costs = costs
        self.clock = clock
        self.recorder = recorder
        self.margin_rate = float(margin_rate)
        self.equity = equity
        self.entry_blackout = entry_blackout
        self.contract = float(costs.instrument.contract_size)
        self.position = 0.0
        self.orders: dict[str, WorkingOrder] = {}
        self.brackets: list[BracketRecord] = []
        self.bracket_bars = 0
        self.ambiguous_bars = 0
        self._last_mid: float | None = None
        self._sigma_ts, self._sigma_values = _sigma_arrays(sigma_1m_bps)

    @property
    def position_lots(self) -> float:
        """The net position (signed lots)."""
        return self.position

    # orders from the engine ---------------------------------------------------------------------

    def submit(self, order: OrderIntent, now: int) -> int | None:
        """Accept an order sent at `now`; return its arrival instant (None: never arrives)."""
        if order.order_id in self.orders:
            raise ValueError(f"order {order.order_id} was already submitted")
        working = WorkingOrder(
            order_id=order.order_id,
            decision_id=order.decision_id,
            intent_id=order.intent_id,
            decided_at=now,
            side=order.side,
            lots=order.size_lots,
            order_type=order.order_type,
            price=order.price,
            role="entry",
            status="sent",
            expected_position=order.expected_position_lots,
            stop=order.stop,
            target=order.target,
        )
        self.orders[order.order_id] = working
        arrival = int(
            self.clock.advance(np.array([now], dtype=np.int64), self.costs.latency.value)[0]
        )
        if arrival == NAT_NS:
            self._close(working, "expired", now, "the order would arrive beyond the market clock")
            return None
        working.arrival = arrival
        return arrival

    def arrive(self, order_id: str, now: int) -> int | None:
        """The order reaches the broker (module docstring); return its expiry timer instant."""
        order = self.orders[order_id]
        if order.status != "sent":
            return None  # cancelled while on its way
        refusal = None
        if order.expected_position is not None and order.expected_position != self.position:
            refusal = (
                f"position changed since the decision (expected {order.expected_position}, "
                f"holds {self.position})"
            )
        after = clean_lots(self.position + order.side.sign * order.lots)
        order.role = "entry" if _increases(self.position, after) else "exit"
        if refusal is None and order.role == "entry":
            refusal = self._margin_refusal(after, now)
            if refusal is None and order.order_type == "market":
                refusal = self._blackout(now)
        if refusal is not None:  # a refused order leaves the working orders alone
            self._close(order, "rejected", now, refusal)
            return None
        for other in list(self.orders.values()):
            if other is not order and other.status in ("sent", "working"):
                self._close(other, "cancelled", now, f"replaced by order {order_id}")
        for bracket in self.brackets:
            if bracket.closed_at is None:
                bracket.closed_at = now
        order.status = "working"
        order.active_from = now
        if order.order_type == "market":
            order.expires_at = now + self.costs.max_fill_delay.value
            return order.expires_at + 1
        return None

    def expire(self, order_id: str, now: int) -> None:
        """Expire a market order that found no timely quote."""
        order = self.orders[order_id]
        if order.status == "working":
            delay = self.costs.max_fill_delay.total_seconds()
            self._close(order, "expired", now, f"no quote within {delay:g} s of arrival")

    def cancel_pending_entries(self, now: int, reason: str) -> None:
        """Cancel orders of earlier intents that have not filled (brackets stay)."""
        for order in list(self.orders.values()):
            if not order.is_bracket and order.status in ("sent", "working"):
                self._close(order, "cancelled", now, reason)

    def finish(self, now: int) -> None:
        """Cancel everything still working at the end of the data."""
        for order in list(self.orders.values()):
            if order.status in ("sent", "working"):
                self._close(order, "cancelled", now, "end of data")
        for bracket in self.brackets:
            if bracket.closed_at is None:
                bracket.closed_at = now

    # market data --------------------------------------------------------------------------------

    def on_market(self, event: TickEvent | ExecutionBarEvent) -> list[Fill]:
        """Fills produced by a quote (tick mode) or an execution bar's open or range (bar mode)."""
        if isinstance(event, TickEvent):
            self._last_mid = (event.bid + event.ask) / 2
            if not self._open(event.ts):
                return []
            return self._on_tick(event)
        bar = event.bar
        if not self._open(bar.start):
            return []
        if event.phase == "open":
            self._last_mid = (bar.bid_open + bar.ask_open) / 2
            fills = self._on_bar_open(event.ts, bar)
        else:
            fills = self._on_bar_range(event.ts, bar)
            self._last_mid = (bar.bid_close + bar.ask_close) / 2
        return fills

    def _on_tick(self, tick: TickEvent) -> list[Fill]:
        fills: list[Fill] = []
        for order in self._working(tick.ts):
            if order.status != "working":
                continue  # cancelled by an earlier fill at this quote (OCO)
            buy = order.side is Side.BUY
            side_price = tick.ask if buy else tick.bid
            if order.order_type == "market":
                assert order.expires_at is not None
                if tick.ts > order.expires_at:
                    self.expire(order.order_id, tick.ts)
                    continue
                if order.role == "entry" and (reason := self._blackout(tick.ts)) is not None:
                    self._close(order, "cancelled", tick.ts, reason)
                    continue
                fill_price, slip = self._slipped(order, side_price, order.decided_at, tick.ts)
            else:
                assert order.price is not None
                if not _triggered(order, tick.bid, tick.ask):
                    continue
                if order.role == "entry" and self._blackout(tick.ts) is not None:
                    continue  # entries wait out a blackout
                if order.order_type == "limit":
                    fills.append(self._limit_fill(order, tick.ts, tick.ask - tick.bid))
                    continue
                fill_price, slip = self._slipped(order, side_price, tick.ts, tick.ts)
            fills.append(
                self._fill(order, tick.ts, side_price, fill_price, tick.bid, tick.ask, slip)
            )
        return fills

    def _on_bar_open(self, ts: int, bar: ExecutionBar) -> list[Fill]:
        fills: list[Fill] = []
        for order in self._working(ts):
            if order.status != "working":
                continue
            buy = order.side is Side.BUY
            side_price = bar.ask_open if buy else bar.bid_open
            if order.order_type == "market":
                assert order.expires_at is not None
                if ts > order.expires_at:
                    self.expire(order.order_id, ts)
                    continue
                if order.role == "entry" and (reason := self._blackout(ts)) is not None:
                    self._close(order, "cancelled", ts, reason)
                    continue
                fill_price, slip = self._slipped(order, side_price, order.decided_at, ts)
            else:
                assert order.price is not None
                if not _triggered(order, bar.bid_open, bar.ask_open):
                    continue
                if order.role == "entry" and self._blackout(ts) is not None:
                    continue
                if order.order_type == "limit":
                    spread = bar.ask_open - bar.bid_open
                    fills.append(self._limit_fill(order, ts, spread, bar.start))
                    continue
                fill_price, slip = self._slipped(order, side_price, ts, ts)  # a gap
            fill = self._fill(
                order, ts, side_price, fill_price, bar.bid_open, bar.ask_open, slip, bar.start
            )
            fills.append(fill)
        return fills

    def _on_bar_range(self, ts: int, bar: ExecutionBar) -> list[Fill]:
        working = [o for o in self._working(ts) if o.order_type != "market"]
        legs = [o for o in working if o.is_bracket]
        if legs:
            self.bracket_bars += 1
        touched = {o.order_id for o in working if _touched(o, bar)}
        ambiguous_parents = {
            o.parent_order_id
            for o in legs
            if o.role == "stop_loss"
            and o.order_id in touched
            and f"{o.parent_order_id}-TP" in touched
        }
        if ambiguous_parents:
            self.ambiguous_bars += 1
        fills: list[Fill] = []
        # stop losses first: with both legs of a bracket touched, the stop is assumed to come first
        for order in sorted(working, key=lambda o: o.role != "stop_loss"):
            if order.status != "working" or order.order_id not in touched:
                continue
            if order.role == "entry" and self._blackout(ts) is not None:
                continue
            assert order.price is not None
            if order.parent_order_id in ambiguous_parents:
                self._bracket_of(order).ambiguous = True
            if order.order_type == "limit":
                fills.append(self._limit_fill(order, ts, bar.spread, bar.start))
                continue
            level = order.price
            bid, ask = _quote_at(order.side, level, bar.spread)
            fill_price, slip = self._slipped(order, level, ts, ts)
            fills.append(self._fill(order, ts, level, fill_price, bid, ask, slip, bar.start))
        return fills

    # helpers -----------------------------------------------------------------------------------

    def _working(self, ts: int) -> list[WorkingOrder]:
        return [o for o in self.orders.values() if o.status == "working" and o.active_from <= ts]

    def _open(self, ts: int) -> bool:
        return bool(self.clock.is_open(np.array([ts], dtype=np.int64))[0])

    def _blackout(self, ts: int) -> str | None:
        if self.entry_blackout is None:
            return None
        reason = self.entry_blackout(ts)
        return None if reason is None else f"entry blackout: {reason}"

    def _margin_refusal(self, after: float, now: int) -> str | None:
        if self._last_mid is None:
            return "no quote known at arrival: margin cannot be checked"
        required = abs(after) * self.contract * self._last_mid * self.margin_rate
        equity = self.equity(now)
        if required > equity:
            return f"insufficient margin: {required:.2f} USD required, equity {equity:.2f} USD"
        return None

    def _sigma(self, at: int) -> float:
        if len(self._sigma_ts) == 0:
            return 0.0
        k = int(np.searchsorted(self._sigma_ts, at, side="right")) - 1
        return float(self._sigma_values[k]) if k >= 0 else 0.0

    def _slipped(
        self, order: WorkingOrder, side_price: float, sigma_at: int, fill_at: int
    ) -> tuple[float, float]:
        slip = self.costs.slippage_bps_at(fill_at, self._sigma(sigma_at))
        sign = order.side.sign
        return side_price * (1 + sign * slip * _BPS), slip

    def _limit_fill(
        self, order: WorkingOrder, ts: int, spread: float, bar_start: int | None = None
    ) -> Fill:
        """A limit order fills at its price: its side of a quote at the level, no slippage."""
        assert order.price is not None
        bid, ask = _quote_at(order.side, order.price, spread)
        return self._fill(order, ts, order.price, order.price, bid, ask, 0.0, bar_start)

    def _fill(
        self,
        order: WorkingOrder,
        ts: int,
        side_price: float,
        fill_price: float,
        bid: float,
        ask: float,
        slip: float,
        bar_start: int | None = None,
    ) -> Fill:
        lots = order.side.sign * order.lots
        mid = (bid + ask) / 2
        before = self.position
        self.position = clean_lots(before + lots)
        order.status = "filled"
        fill = Fill(
            fill_id=f"F-{order.order_id}",
            order_id=order.order_id,
            decision_id=order.decision_id,
            intent_id=order.intent_id,
            ts=ts,
            decided_at=order.decided_at,
            role=order.role,
            order_type=order.order_type,
            lots=lots,
            price=fill_price,
            bid=bid,
            ask=ask,
            mid=mid,
            slippage_bps=slip,
            spread_cost=order.lots * abs(side_price - mid) * self.contract,
            slippage_cost=lots * (fill_price - side_price) * self.contract,
            commission=float(self.costs.commission_usd([order.lots], [mid])[0]),
            position_after=self.position,
            bar_start=bar_start,
        )
        self.recorder.fill(fill)
        if order.is_bracket:
            self._leg_filled(order, ts)
        elif self.position == 0:
            self._cancel_legs(ts, "position closed")
        if not order.is_bracket and self.position != 0 and (order.stop or order.target):
            self._place_bracket(order, ts)
        return fill

    def _place_bracket(self, parent: WorkingOrder, ts: int) -> None:
        self._cancel_legs(ts, f"replaced by the bracket of {parent.order_id}")
        held = Side.BUY if self.position > 0 else Side.SELL
        closing = held.opposite
        size = abs(self.position)
        legs: list[tuple[str, OrderType, float | None, Role]] = [
            ("SL", "stop", parent.stop, "stop_loss"),
            ("TP", "limit", parent.target, "take_profit"),
        ]
        for suffix, kind, level, role in legs:
            if level is None:
                continue
            leg = WorkingOrder(
                order_id=f"{parent.order_id}-{suffix}",
                decision_id=parent.decision_id,
                intent_id=parent.intent_id,
                decided_at=parent.decided_at,
                side=closing,
                lots=size,
                order_type=kind,
                price=level,
                role=role,
                status="working",
                parent_order_id=parent.order_id,
                arrival=ts,
                active_from=ts + 1,
            )
            self.orders[leg.order_id] = leg
            self.recorder.bracket_order(
                order_id=leg.order_id,
                parent_order_id=parent.order_id,
                decision_id=parent.decision_id,
                intent_id=parent.intent_id,
                ts=ts,
                side=closing,
                lots=size,
                order_type=kind,
                price=level,
                role=role,
            )
        self.brackets.append(
            BracketRecord(parent.order_id, held, parent.stop, parent.target, ts + 1)
        )

    def _leg_filled(self, leg: WorkingOrder, ts: int) -> None:
        bracket = self._bracket_of(leg)
        bracket.closed_at = ts
        bracket.exit_role = leg.role
        for order in self.orders.values():
            if order.parent_order_id == leg.parent_order_id and order.status == "working":
                self._close(order, "cancelled", ts, f"OCO: {leg.order_id} filled")

    def _cancel_legs(self, ts: int, reason: str) -> None:
        for order in list(self.orders.values()):
            if order.is_bracket and order.status == "working":
                self._close(order, "cancelled", ts, reason)
        for bracket in self.brackets:
            if bracket.closed_at is None:
                bracket.closed_at = ts

    def _bracket_of(self, leg: WorkingOrder) -> BracketRecord:
        for bracket in reversed(self.brackets):
            if bracket.parent_order_id == leg.parent_order_id:
                return bracket
        raise KeyError(leg.parent_order_id)

    def _close(self, order: WorkingOrder, status: Status, ts: int, reason: str) -> None:
        order.status = status
        if status == "cancelled":
            self.recorder.order_cancelled(order.order_id, ts, reason)
        elif status == "rejected":
            self.recorder.order_rejected(order.order_id, ts, reason)
        else:
            self.recorder.order_expired(order.order_id, ts, reason)


def _increases(before: float, after: float) -> bool:
    """True if moving from `before` to `after` opens, increases or flips exposure."""
    return abs(after) > abs(before) or (after != 0 and np.sign(after) != np.sign(before))


def _quote_at(side: Side, level: float, spread: float) -> tuple[float, float]:
    """The bid and ask of a quote whose `side` price (bid for a sell, ask for a buy) is `level`."""
    return (level, level + spread) if side is Side.SELL else (level - spread, level)


def _triggered(order: WorkingOrder, bid: float, ask: float) -> bool:
    """Whether a stop or limit order triggers at this quote."""
    assert order.price is not None
    if order.order_type == "stop":
        return ask >= order.price if order.side is Side.BUY else bid <= order.price
    return ask <= order.price if order.side is Side.BUY else bid >= order.price


def _touched(order: WorkingOrder, bar: ExecutionBar) -> bool:
    """Whether a stop or limit order's level lies within the bar's range on its side."""
    assert order.price is not None
    if order.side is Side.BUY:
        return (
            bar.ask_high >= order.price
            if order.order_type == "stop"
            else bar.ask_low <= order.price
        )
    return bar.bid_low <= order.price if order.order_type == "stop" else bar.bid_high >= order.price


def _sigma_arrays(sigma: pd.Series | None) -> tuple[np.ndarray, np.ndarray]:
    if sigma is None or len(sigma) == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float64)
    index = ensure_utc_index(pd.DatetimeIndex(sigma.index))
    order = np.argsort(index.to_numpy("datetime64[ns]").view(np.int64), kind="stable")
    ts = index.to_numpy("datetime64[ns]").view(np.int64)[order]
    values = sigma.to_numpy(np.float64)[order]
    if np.any(values < 0) or not np.isfinite(values).all():
        raise ValueError("sigma-hat must be finite and non-negative")
    return ts, values
