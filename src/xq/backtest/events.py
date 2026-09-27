"""Event core of the event-driven backtester (BT-004).

Every event carries an instant ``ts`` in UTC int64 nanoseconds. The `EventQueue` orders events by
``(ts, rank, sequence)``: time first; at the same instant by a fixed rank of the event's kind; and
among events of the same instant and kind by the order they were pushed (the sequence). Ties are
therefore broken deterministically, and a run is a pure function of its inputs.

The ranks at one instant (`Rank`) encode what "at or after" means for the execution rules:

1. ``TIMER`` — scheduled engine timers (financing at a rollover, a trading day's end, an order's
   expiry, a time stop): they see the state *before* any market data at that instant, so a
   rollover charges the position held over it and a day's end marks with the last quote strictly
   before it (as the vectorized screener does, BT-002).
2. ``ORDER`` — an order arriving at the broker: a quote at exactly the arrival instant can fill it
   (the first quote at or after arrival).
3. ``MARKET`` — a quote (`TickEvent`) or, without ticks, an execution bar (`ExecutionBarEvent`).
4. ``FILL`` — a fill is booked into the account.
5. ``BAR`` — a signal bar becomes available (at its ``available_at``); the strategy decides.
6. ``SIGNAL`` — a strategy's intent goes through the session constraints and the risk approver.

`SimulationClock` is the engine's clock. It moves only when an event is processed and never goes
backwards, so nothing can be scheduled in the past.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, ClassVar, Literal

if TYPE_CHECKING:
    from xq.signals.schema import TradeIntent


class Rank(IntEnum):
    """Processing order of event kinds at the same instant (module docstring)."""

    TIMER = 0
    ORDER = 1
    MARKET = 2
    FILL = 3
    BAR = 4
    SIGNAL = 5


# --- market state -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Quote:
    """A bid/ask quote at an instant."""

    ts: int
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        """The mid price (for marking and sizing only; fills never happen at mid)."""
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class Bar:
    """A signal bar as a strategy sees it: mid OHLC over ``[start, end)``, known at available_at."""

    start: int
    end: int
    available_at: int
    open: float
    high: float
    low: float
    close: float
    bid_close: float
    ask_close: float


@dataclass(frozen=True, slots=True)
class ExecutionBar:
    """A bid/ask bar the broker executes against when there are no ticks (bar mode)."""

    start: int
    end: int
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    spread: float


@dataclass(frozen=True, slots=True)
class AccountState:
    """What the account holds at an instant (the risk approver's view of the account)."""

    ts: int
    capital: float
    cash: float
    unrealized: float
    equity: float
    position_lots: float
    margin_used: float


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution, with its cost decomposition against the reference mid.

    ``lots`` is signed (positive buys). ``bid``/``ask`` are the quote the fill executed against
    and ``mid`` their mid; ``lots * (price - mid) * contract_size = spread_cost + slippage_cost``
    holds exactly, where the spread cost is the half-spread and the slippage cost the rest.
    """

    fill_id: str
    order_id: str
    decision_id: str
    intent_id: str
    ts: int
    decided_at: int
    role: str
    order_type: str
    lots: float
    price: float
    bid: float
    ask: float
    mid: float
    slippage_bps: float
    spread_cost: float
    slippage_cost: float
    commission: float
    position_after: float
    bar_start: int | None = None


# --- events -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickEvent:
    """A new quote."""

    rank: ClassVar[Rank] = Rank.MARKET
    ts: int
    bid: float
    ask: float

    @property
    def quote(self) -> Quote:
        """The quote this event carries."""
        return Quote(self.ts, self.bid, self.ask)


@dataclass(frozen=True, slots=True)
class ExecutionBarEvent:
    """An execution bar in bar mode: its open at the bar's start, its range just before its end.

    ``phase == "open"`` is processed at ``bar.start`` (the first price of the bar); ``"range"`` at
    ``bar.end - 1`` (every price of the bar is then known to the broker, never to the strategy).
    """

    rank: ClassVar[Rank] = Rank.MARKET
    ts: int
    bar: ExecutionBar
    phase: Literal["open", "range"]


@dataclass(frozen=True, slots=True)
class BarEvent:
    """A signal bar becomes available to the strategy (``ts == bar.available_at``)."""

    rank: ClassVar[Rank] = Rank.BAR
    ts: int
    bar: Bar


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """A strategy's intent, decided at ``ts``."""

    rank: ClassVar[Rank] = Rank.SIGNAL
    ts: int
    intent: TradeIntent


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """An order reaches the broker (after the latency, in market time)."""

    rank: ClassVar[Rank] = Rank.ORDER
    ts: int
    order_id: str


@dataclass(frozen=True, slots=True)
class FillEvent:
    """A fill to book into the account."""

    rank: ClassVar[Rank] = Rank.FILL
    ts: int
    fill: Fill


TimerName = Literal["rollover", "day_end", "expire", "time_stop", "flat_before_weekend"]


@dataclass(frozen=True, slots=True)
class TimerEvent:
    """A scheduled engine timer; `ref` names what it refers to (an order or intent id)."""

    rank: ClassVar[Rank] = Rank.TIMER
    ts: int
    name: TimerName
    ref: str | None = None
    multiplier: int = 1


Event = TickEvent | ExecutionBarEvent | BarEvent | SignalEvent | OrderEvent | FillEvent | TimerEvent
EventKey = tuple[int, int, int]


class EventQueue:
    """Priority queue of events ordered by ``(ts, rank, sequence)`` (module docstring)."""

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, int, Event]] = []
        self._sequence = itertools.count()

    def push(self, event: Event) -> None:
        """Add `event`; among equal (ts, rank) it comes after everything pushed before it."""
        heapq.heappush(self._heap, (event.ts, int(event.rank), next(self._sequence), event))

    def pop(self) -> Event:
        """Remove and return the first event.

        Raises:
            IndexError: if the queue is empty.
        """
        return heapq.heappop(self._heap)[3]

    def peek_key(self) -> EventKey | None:
        """``(ts, rank, sequence)`` of the first event, or None when empty."""
        if not self._heap:
            return None
        ts, rank, sequence, _ = self._heap[0]
        return ts, rank, sequence

    def __len__(self) -> int:
        return len(self._heap)


class SimulationClock:
    """The engine's deterministic clock: moved only by processed events, never backwards."""

    def __init__(self, start: int) -> None:
        self._now = int(start)

    @property
    def now(self) -> int:
        """The current instant (UTC nanoseconds)."""
        return self._now

    def advance_to(self, ts: int) -> None:
        """Move the clock to `ts`.

        Raises:
            ValueError: if `ts` is earlier than the current instant.
        """
        if ts < self._now:
            raise ValueError(f"events must not go back in time: {ts} < {self._now}")
        self._now = int(ts)


#: Positions and order sizes are rounded to this many decimals after arithmetic, so a position
#: built from lot-step multiples returns to exactly zero (float sums otherwise leave 1e-17).
LOT_DECIMALS = 9


def clean_lots(value: float) -> float:
    """`value` rounded to `LOT_DECIMALS` decimals, with negative zero turned into zero."""
    return round(float(value), LOT_DECIMALS) + 0.0
