"""Event-driven backtest engine: data source, strategy interface and the event loop (BT-004).

`EventEngine(strategy, data, broker, risk, clock, ...)` replays market data through the decision
chain the plan prescribes (section 6, Phase 15):

    strategy.on_bar -> TradeIntent -> session constraints -> RiskEngine.evaluate -> RiskDecision
        -> OrderIntent (only from an approved, issued decision) -> broker -> Fill -> account

Nothing reaches the broker without a decision of the risk engine (`xq.risk.engine.RiskEngine`,
RISK-005), and every step is written to the recorder (the decision ledger, BT-007).

**Risk and market state.** At every decision the engine observes the account into the risk state
(`RiskStateTracker`, RISK-001) and assembles the market state (`MarketState`): the latest quote,
the daily sigma-hat known at the decision (a supplied series or the interim EWMA of signal-bar
returns, `xq.risk.state`) and the sessions the risk profile caps. The strategy's context carries
the same sigma-hat, so stops are set in volatility units.

**Data** (`MarketData`) is either quotes (*tick mode*: the broker executes on every quote and
signal bars are built from the same quotes by the DATA-008 bar builder) or bars only (*bar mode*:
the broker executes on one-minute bid/ask bars, opening price first, then the bar's range). The
strategy only ever sees completed signal bars at their ``available_at``; the broker sees prices as
they happen, never the strategy.

**Decision time** is the ``available_at`` of the bar the strategy is handed, and the engine stamps
it (with an intent id) on every intent: a strategy cannot choose its decision time. A decision
taken while the market is closed places no order (ADR 0032); it is refused and recorded.

**Timers.** The engine schedules financing at every rollover (`CostModel.rollovers`), a snapshot
at the end of every trading day with data, the expiry of market orders that find no quote within
the maximum fill delay, time stops and, when configured, the flat-before-weekend exit. Time stops
and weekend exits are intents like any other: they go through the risk engine (which always
approves an exit).

The loop merges the data stream with the event queue by ``(ts, rank)`` (`xq.backtest.events`),
so the whole run is deterministic: the same inputs give the same events in the same order.
"""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

import numpy as np
import pandas as pd

from xq.backtest.broker_sim import BracketRecord, SimulatedBroker, through
from xq.backtest.costs import CostModel
from xq.backtest.events import (
    AccountState,
    Bar,
    BarEvent,
    Event,
    EventQueue,
    ExecutionBar,
    ExecutionBarEvent,
    Fill,
    FillEvent,
    OrderEvent,
    Quote,
    SignalEvent,
    SimulationClock,
    TickEvent,
    TimerEvent,
)
from xq.backtest.ledger import Ledger
from xq.backtest.portfolio import Portfolio, daily_frame, fills_frame
from xq.backtest.vectorized import BacktestResult
from xq.core.errors import NaiveTimestampError
from xq.core.time import from_ns, trading_day, trading_day_bounds, trading_days
from xq.core.types import Side, Timeframe
from xq.data.bars import build_bars
from xq.data.calendar import NAT_NS, MarketClock, regular_trading_day
from xq.data.sessions import build_session_table
from xq.risk.engine import RiskEngine
from xq.risk.state import (
    EwmaSigma,
    MarketState,
    RiskState,
    RiskStateTracker,
    SeriesSigma,
    SigmaSource,
)
from xq.signals.schema import OrderIntent, RiskDecision, TradeIntent

#: Execution bars in bar mode are one-minute bars (the plan keeps 1m data for execution).
EXECUTION_TIMEFRAME = Timeframe.M1
DataMode = Literal["ticks", "bars"]
_QUOTE_COLUMNS = ("ts_utc", "bid", "ask")
_BAR_PRICE_COLUMNS = tuple(
    f"{b}_{p}" for b in ("bid", "ask") for p in ("open", "high", "low", "close")
)


# --- market data --------------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketData:
    """What the engine replays: signal bars plus quotes (tick mode) or execution bars (bar mode).

    Instants are UTC int64 nanoseconds. ``bars`` are signal bars of ``timeframe`` in the DATA-008
    layout (``bar_start_utc``, ``available_at_utc``, bid/ask/mid OHLC); ``quotes`` hold
    ``ts_utc``, ``bid`` and ``ask``; ``execution_bars`` are one-minute bid/ask bars. ``minute_bars``
    are the one-minute bars used to report how often bars alone could not have resolved a stop
    and a target (built from the quotes in tick mode, the execution bars in bar mode).
    """

    bars: pd.DataFrame
    timeframe: Timeframe
    quotes: pd.DataFrame | None = None
    execution_bars: pd.DataFrame | None = None
    minute_bars: pd.DataFrame = field(default_factory=pd.DataFrame)

    @classmethod
    def from_ticks(
        cls,
        quotes: pd.DataFrame,
        timeframe: Timeframe,
        *,
        publication_latency_ms: int = 0,
        coverage_end: pd.Timestamp | None = None,
    ) -> MarketData:
        """Tick-mode data: the quotes and signal bars built from them (DATA-008 `build_bars`).

        Args:
            quotes: ``ts_utc`` (tz-aware timestamps or UTC int64 nanoseconds), ``bid``, ``ask``,
                sorted by time.
            timeframe: Signal-bar timeframe.
            publication_latency_ms: Added to each bar's end to give ``available_at``.
            coverage_end: The instant the quotes are complete up to; bars ending later are
                dropped. Default: the end of the trading day of the last quote.
        """
        clean = _checked_quotes(quotes)
        ts = clean["ts_utc"].to_numpy(np.int64)
        if coverage_end is None:
            end = trading_day_bounds(
                trading_days(pd.DatetimeIndex([from_ns(int(ts[-1]))]))[0].item()
            )[1]
        else:
            end = pd.Timestamp(coverage_end)
            if end.tzinfo is None:
                raise NaiveTimestampError("coverage_end must be tz-aware")
        ticks = clean.assign(flags=np.zeros(len(clean), dtype=np.uint32))
        latency = publication_latency_ms * 1_000_000

        def bars_of(tf: Timeframe) -> pd.DataFrame:
            built = build_bars(
                ticks, tf, exclude_flags=0, latency_ns=latency, coverage_end_ns=end.value
            )
            return built.loc[built["is_complete"]].reset_index(drop=True)

        return cls(bars_of(timeframe), timeframe, clean, None, bars_of(EXECUTION_TIMEFRAME))

    @classmethod
    def from_bars(
        cls, bars: pd.DataFrame, timeframe: Timeframe, *, execution_bars: pd.DataFrame
    ) -> MarketData:
        """Bar-mode data: signal bars and one-minute execution bars, both in DATA-008 layout."""
        signal = _checked_bars(bars, "bars")
        execution = _checked_bars(execution_bars, "execution_bars")
        return cls(signal, timeframe, None, execution, execution)

    @property
    def mode(self) -> DataMode:
        """``ticks`` when quotes drive execution, ``bars`` otherwise."""
        return "ticks" if self.quotes is not None else "bars"

    @property
    def start(self) -> int:
        """The first market-data instant."""
        return int(self._market_times()[0])

    @property
    def end(self) -> int:
        """The last market-data instant."""
        return int(self._market_times()[-1])

    def trading_days(self) -> list[date]:
        """Trading days (17:00 New York roll) with market data, in order."""
        days = np.unique(
            trading_days(
                pd.DatetimeIndex(pd.to_datetime(self._market_times(), unit="ns", utc=True))
            )
        )
        return [d.item() for d in days]

    def events(self) -> Iterator[Event]:
        """Market data and signal bars as events, sorted by ``(ts, rank)`` (module docstring)."""
        market = self._market_events()
        bars = _signal_bars(self.bars, self.timeframe)
        signal = (BarEvent(bar.available_at, bar) for bar in bars)
        next_market, next_bar = next(market, None), next(signal, None)
        while next_market is not None or next_bar is not None:
            if next_bar is None or (next_market is not None and next_market.ts <= next_bar.ts):
                assert next_market is not None
                yield next_market
                next_market = next(market, None)
            else:
                yield next_bar
                next_bar = next(signal, None)

    def _market_events(self) -> Iterator[TickEvent | ExecutionBarEvent]:
        if self.quotes is not None:
            ts = self.quotes["ts_utc"].to_numpy(np.int64)
            bid = self.quotes["bid"].to_numpy(np.float64)
            ask = self.quotes["ask"].to_numpy(np.float64)
            for i in range(len(ts)):
                yield TickEvent(int(ts[i]), float(bid[i]), float(ask[i]))
            return
        assert self.execution_bars is not None
        for bar in _execution_bars(self.execution_bars):
            yield ExecutionBarEvent(bar.start, bar, "open")
            yield ExecutionBarEvent(bar.end - 1, bar, "range")

    def _market_times(self) -> np.ndarray:
        if self.quotes is not None:
            times = self.quotes["ts_utc"].to_numpy(np.int64)
        else:
            assert self.execution_bars is not None
            times = self.execution_bars["bar_start_utc"].to_numpy(np.int64)
        if len(times) == 0:
            raise ValueError("market data is empty")
        return times


def _checked_quotes(quotes: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in _QUOTE_COLUMNS if c not in quotes.columns]
    if missing:
        raise ValueError(f"quotes lack columns {missing}")
    if len(quotes) == 0:
        raise ValueError("quotes are empty")
    column = quotes["ts_utc"]
    if pd.api.types.is_datetime64_any_dtype(column):
        index = pd.DatetimeIndex(column)
        if index.tz is None:
            raise NaiveTimestampError("quote timestamps must be tz-aware (or UTC nanoseconds)")
        ts = index.tz_convert("UTC").as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
    elif pd.api.types.is_integer_dtype(column):
        ts = column.to_numpy(np.int64)
    else:
        raise ValueError("ts_utc must be tz-aware timestamps or UTC int64 nanoseconds")
    if np.any(np.diff(ts) < 0):
        raise ValueError("quotes must be in time order")
    bid = quotes["bid"].to_numpy(np.float64)
    ask = quotes["ask"].to_numpy(np.float64)
    if not (np.isfinite(bid).all() and np.isfinite(ask).all()) or np.any(bid <= 0):
        raise ValueError("quotes need finite, positive bid and ask prices")
    if np.any(ask < bid):
        raise ValueError("quotes must not be crossed (ask below bid)")
    return pd.DataFrame({"ts_utc": ts, "bid": bid, "ask": ask})


def _checked_bars(bars: pd.DataFrame, name: str) -> pd.DataFrame:
    required = ("bar_start_utc", "available_at_utc", *_BAR_PRICE_COLUMNS)
    missing = [c for c in required if c not in bars.columns]
    if missing:
        raise ValueError(f"{name} lack columns {missing}")
    if len(bars) == 0:
        raise ValueError(f"{name} are empty")
    for column in ("bar_start_utc", "available_at_utc"):
        if not pd.api.types.is_integer_dtype(bars[column]):
            raise ValueError(f"{name}.{column} must be UTC int64 nanoseconds")
    if np.any(np.diff(bars["bar_start_utc"].to_numpy(np.int64)) <= 0):
        raise ValueError(f"{name} must be sorted by bar_start_utc without duplicates")
    frame = bars.reset_index(drop=True).copy()
    for basis in ("mid",):
        for part in ("open", "high", "low", "close"):
            column = f"{basis}_{part}"
            if column not in frame.columns:
                frame[column] = (frame[f"bid_{part}"] + frame[f"ask_{part}"]) / 2
    return frame


def _signal_bars(bars: pd.DataFrame, timeframe: Timeframe) -> Iterator[Bar]:
    start = bars["bar_start_utc"].to_numpy(np.int64)
    available = bars["available_at_utc"].to_numpy(np.int64)
    names = ("mid_open", "mid_high", "mid_low", "mid_close", "bid_close", "ask_close")
    columns = {c: bars[c].to_numpy(np.float64) for c in names}
    for i in range(len(start)):
        yield Bar(
            start=int(start[i]),
            end=int(start[i]) + timeframe.nanos,
            available_at=int(available[i]),
            open=float(columns["mid_open"][i]),
            high=float(columns["mid_high"][i]),
            low=float(columns["mid_low"][i]),
            close=float(columns["mid_close"][i]),
            bid_close=float(columns["bid_close"][i]),
            ask_close=float(columns["ask_close"][i]),
        )


def _execution_bars(bars: pd.DataFrame) -> Iterator[ExecutionBar]:
    start = bars["bar_start_utc"].to_numpy(np.int64)
    values = {c: bars[c].to_numpy(np.float64) for c in _BAR_PRICE_COLUMNS}
    if "spread_med" in bars.columns:
        spread = bars["spread_med"].to_numpy(np.float64)
    else:
        spread = values["ask_open"] - values["bid_open"]
    length = EXECUTION_TIMEFRAME.nanos
    for i in range(len(start)):
        v = {c: float(values[c][i]) for c in _BAR_PRICE_COLUMNS}
        yield ExecutionBar(
            start=int(start[i]),
            end=int(start[i]) + length,
            bid_open=v["bid_open"],
            bid_high=v["bid_high"],
            bid_low=v["bid_low"],
            bid_close=v["bid_close"],
            ask_open=v["ask_open"],
            ask_high=v["ask_high"],
            ask_low=v["ask_low"],
            ask_close=v["ask_close"],
            spread=float(spread[i]),
        )


# --- strategy interface -------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyContext:
    """What a strategy may know when it decides: the time, its account, the latest quote and the
    daily sigma-hat (a fraction of price) the risk engine will use, None while unknown."""

    now: int
    account: AccountState
    quote: Quote | None
    sigma_daily: float | None = None

    @property
    def time(self) -> pd.Timestamp:
        """The decision time as a UTC timestamp."""
        return from_ns(self.now)


class Strategy(ABC):
    """A strategy turns completed signal bars into trade intents; it never sizes or orders.

    `on_bar` is called once per signal bar at its ``available_at`` (the decision time) with the
    bar and the context; it returns the intents to act on (usually none or one). `on_fill`
    reports every fill of the account, including stops and targets, after it is booked.
    """

    strategy_id: str = "strategy"
    version: str = "1"

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[TradeIntent]:
        """Intents decided at ``ctx.now`` from the bars seen so far."""

    def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:  # noqa: B027 - optional hook
        """Called after each fill is booked (default: nothing)."""


# --- engine components --------------------------------------------------------------------------


class Broker(Protocol):
    """Receives orders, matches them against market data and returns fills (BT-005)."""

    @property
    def position_lots(self) -> float:
        """The broker's net position."""
        ...

    def submit(self, order: OrderIntent, now: int) -> int | None:
        """Accept `order` sent at `now`; return its arrival instant (None: it never arrives)."""
        ...

    def arrive(self, order_id: str, now: int) -> int | None:
        """The order reaches the broker; return the instant it expires, if it can expire."""
        ...

    def on_market(self, event: TickEvent | ExecutionBarEvent) -> list[Fill]:
        """Match working orders against a quote or an execution bar."""
        ...

    def expire(self, order_id: str, now: int) -> None:
        """Expire `order_id` if it is still working."""
        ...

    def cancel_pending_entries(self, now: int, reason: str) -> None:
        """Cancel working entry orders that have not filled (a newer intent supersedes them)."""
        ...

    def finish(self, now: int) -> None:
        """The data has ended: cancel whatever is still working."""
        ...


class Account(Protocol):
    """Books fills and financing and values the position (BT-006)."""

    def mark(self, quote: Quote) -> None:
        """Value the position at this quote's mid from now on."""
        ...

    def book(self, fill: Fill) -> None:
        """Book a fill."""
        ...

    def charge_financing(self, ts: int, multiplier: int) -> float:
        """Charge financing on the position held over a rollover; return the charge."""
        ...

    def state(self, ts: int) -> AccountState:
        """The account at `ts`."""
        ...


class Recorder(Protocol):
    """The decision ledger (BT-007): every step of the chain, with linking ids."""

    def intent(self, intent: TradeIntent) -> None:
        """A strategy or engine intent was decided."""
        ...

    def refusal(self, intent: TradeIntent, reason: str) -> None:
        """An intent was refused before the risk decision (session constraints)."""
        ...

    def decision(self, decision: RiskDecision) -> None:
        """The risk engine decided an intent."""
        ...

    def order(self, order: OrderIntent, *, submitted_at: int, arrival: int | None) -> None:
        """An order was sent to the broker (the broker records what happens to it)."""
        ...

    def account(self, account: AccountState, event: str) -> None:
        """The account the risk state observed (at a decision or a trading day's end)."""
        ...


class Constraints(Protocol):
    """Session constraints on entries (BT-008)."""

    def refuse(self, intent: TradeIntent, now: int, position_lots: float) -> str | None:
        """Why `intent` may not be acted on at `now`, or None."""
        ...

    def flat_times(self, start: int, end: int) -> list[int]:
        """Instants in ``[start, end]`` at which every position is closed (flat before weekend)."""
        ...

    def entry_blackout(self, ts: int) -> str | None:
        """Why no entry may fill at `ts`, or None (the broker asks at arrival and at fills)."""
        ...


class NoConstraints:
    """No session constraints (only the closed-market rule, which the engine always applies)."""

    def refuse(self, intent: TradeIntent, now: int, position_lots: float) -> str | None:
        """Nothing is refused."""
        return None

    def entry_blackout(self, ts: int) -> str | None:
        """No blackouts."""
        return None

    def flat_times(self, start: int, end: int) -> list[int]:
        """No forced exits."""
        return []


@dataclass(frozen=True)
class DaySnapshot:
    """The account at the end of a trading day (after that day's rollover charge)."""

    day: date
    state: AccountState


@dataclass
class EngineOutput:
    """What one engine run observed (fills, snapshots, financing charges, refusals)."""

    fills: list[Fill] = field(default_factory=list)
    bar_states: list[AccountState] = field(default_factory=list)
    day_states: list[DaySnapshot] = field(default_factory=list)
    financing: list[tuple[int, int, float]] = field(default_factory=list)
    refused: list[tuple[int, str, str]] = field(default_factory=list)
    risk_states: list[RiskState] = field(default_factory=list)
    events: int = 0


# --- the loop -----------------------------------------------------------------------------------


class EventEngine:
    """Replays `data` through strategy, constraints, risk engine, broker and account.

    `sigma` gives the daily sigma-hat known at each instant; by default the interim EWMA of the
    signal bars with the risk profile's span (`xq.risk.state.EwmaSigma`).
    """

    def __init__(
        self,
        strategy: Strategy,
        data: MarketData,
        *,
        broker: Broker,
        risk: RiskEngine,
        account: Account,
        recorder: Recorder,
        costs: CostModel,
        clock: MarketClock,
        constraints: Constraints | None = None,
        sigma: SigmaSource | None = None,
        observer: Callable[[Event, int], None] | None = None,
    ) -> None:
        if not isinstance(risk, RiskEngine):
            raise TypeError("every intent is decided by RiskEngine.evaluate (RISK-005)")
        known = {*costs.sessions.sessions, *costs.sessions.overlaps}
        unknown = sorted(set(risk.watched_sessions) - known)
        if unknown:
            raise ValueError(f"the risk profile caps unknown sessions {unknown}")
        self.strategy = strategy
        self.data = data
        self.broker = broker
        self.risk = risk
        self.account = account
        self.recorder = recorder
        self.costs = costs
        self.market_clock = clock
        self.constraints: Constraints = constraints if constraints is not None else NoConstraints()
        self.observer = observer
        self.clock = SimulationClock(trading_day_bounds(data.trading_days()[0])[0].value)
        self.queue = EventQueue()
        self.output = EngineOutput()
        self.last_quote: Quote | None = None
        self._intent_ids = itertools.count(1)
        self._orders: dict[str, OrderIntent] = {}
        self._position_intent: str | None = None
        self._ran = False
        self.risk_state = RiskStateTracker(
            account.state(self.clock.now).capital, float(costs.instrument.contract_size)
        )
        if sigma is None:
            per_day = regular_trading_day(costs.sessions) / data.timeframe.duration
            sigma = EwmaSigma(risk.config.sigma.span_bars, risk.config.sigma.min_bars, per_day)
        self.sigma = sigma
        self._session_windows: dict[date, dict[str, tuple[int, int]]] = {}

    def run(self) -> EngineOutput:
        """Process every event once (an engine runs once)."""
        if self._ran:
            raise RuntimeError("an EventEngine runs once; build a new one")
        self._ran = True
        last_day_end = self._schedule_timers()
        stream = self.data.events()
        head = next(stream, None)
        while True:
            queued = self.queue.peek_key()
            if head is not None and (queued is None or (head.ts, int(head.rank)) <= queued[:2]):
                event, head = head, next(stream, None)
            elif queued is not None and queued[0] <= last_day_end:
                event = self.queue.pop()
            else:
                break
            self.clock.advance_to(event.ts)
            self._dispatch(event)
            self.output.events += 1
            if self.observer is not None:
                self.observer(event, self.clock.now)
        self.broker.finish(self.clock.now)
        return self.output

    # scheduling ---------------------------------------------------------------------------------

    def _schedule_timers(self) -> int:
        days = self.data.trading_days()
        start = trading_day_bounds(days[0])[0]
        end = trading_day_bounds(days[-1])[1]
        # rollovers after the first day's start, up to and including the last day's end
        rolls = self.costs.rollovers(start + pd.Timedelta(1, "ns"), end + pd.Timedelta(1, "ns"))
        instants = (
            pd.DatetimeIndex(rolls.index).as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
        )
        for instant, multiplier in zip(
            instants.tolist(), rolls.to_numpy(np.int64).tolist(), strict=True
        ):
            self.queue.push(TimerEvent(instant, "rollover", multiplier=multiplier))
        for day in days:
            self.queue.push(
                TimerEvent(trading_day_bounds(day)[1].value, "day_end", ref=day.isoformat())
            )
        for flat in self.constraints.flat_times(start.value, end.value):
            self.queue.push(TimerEvent(flat, "flat_before_weekend"))
        return int(end.value)

    # dispatch -----------------------------------------------------------------------------------

    def _dispatch(self, event: Event) -> None:
        if isinstance(event, TickEvent):
            self._on_quote(event.quote)
            self._push_fills(self.broker.on_market(event))
        elif isinstance(event, ExecutionBarEvent):
            bar = event.bar
            if event.phase == "open":
                self._on_quote(Quote(event.ts, bar.bid_open, bar.ask_open))
                self._push_fills(self.broker.on_market(event))
            else:
                self._push_fills(self.broker.on_market(event))
                self._on_quote(Quote(event.ts, bar.bid_close, bar.ask_close))
        elif isinstance(event, FillEvent):
            self._on_fill(event.fill)
        elif isinstance(event, BarEvent):
            self._on_bar(event)
        elif isinstance(event, SignalEvent):
            self._on_intent(event.intent)
        elif isinstance(event, OrderEvent):
            expiry = self.broker.arrive(event.order_id, event.ts)
            if expiry is not None:
                self.queue.push(TimerEvent(expiry, "expire", ref=event.order_id))
        else:
            self._on_timer(event)

    def _on_quote(self, quote: Quote) -> None:
        self.last_quote = quote
        self.account.mark(quote)

    def _push_fills(self, fills: list[Fill]) -> None:
        for fill in fills:
            self.queue.push(FillEvent(fill.ts, fill))

    def _on_fill(self, fill: Fill) -> None:
        self.account.book(fill)
        self.risk_state.on_fill(fill)
        self.output.fills.append(fill)
        order = self._orders.get(fill.order_id)
        if order is not None:  # an order the engine sent (not a bracket leg)
            self._position_intent = order.intent_id if fill.position_after != 0 else None
            if order.time_stop is not None and fill.position_after != 0:
                at = max(pd.Timestamp(order.time_stop).value, self.clock.now)
                self.queue.push(TimerEvent(at, "time_stop", ref=order.intent_id))
        elif fill.position_after == 0:
            self._position_intent = None
        self.strategy.on_fill(fill, self._context())

    def _on_bar(self, event: BarEvent) -> None:
        self.sigma.update(event.bar.close)
        context = self._context()
        self.output.bar_states.append(context.account)
        for intent in self.strategy.on_bar(event.bar, context):
            self._push_intent(intent)

    def _push_intent(self, intent: TradeIntent) -> None:
        stamped = intent.model_copy(
            update={
                "intent_id": f"I{next(self._intent_ids):06d}",
                "created_at": from_ns(self.clock.now),
            }
        )
        self.queue.push(SignalEvent(self.clock.now, stamped))

    def _on_intent(self, intent: TradeIntent) -> None:
        now = self.clock.now
        self.recorder.intent(intent)
        account = self.account.state(now)
        self.risk_state.observe(account)
        self.recorder.account(account, "decision")
        state = self.risk_state.state()
        self.output.risk_states.append(state)
        position = self.broker.position_lots
        if state.position_lots != position:
            raise RuntimeError(
                f"the risk state holds {state.position_lots} lots, the broker {position}"
            )
        if not bool(self.market_clock.is_open(np.array([now], dtype=np.int64))[0]):
            reason: str | None = "market closed at the decision time: no order (ADR 0032)"
        else:
            reason = self.constraints.refuse(intent, now, position)
        if reason is not None:
            self.recorder.refusal(intent, reason)
            self.output.refused.append((now, str(intent.intent_id), reason))
            return
        decision = self.risk.evaluate(intent, state, self._market(now))
        self.recorder.decision(decision)
        if not decision.approved:
            return
        if decision.side is None:
            self.broker.cancel_pending_entries(now, f"superseded by intent {intent.intent_id}")
            return
        order = OrderIntent.from_decision(decision, intent, expected_position_lots=position)
        self._orders[order.order_id] = order
        arrival = self.broker.submit(order, now)
        self.recorder.order(order, submitted_at=now, arrival=arrival)
        if arrival is not None:
            self.queue.push(OrderEvent(arrival, order.order_id))

    def _on_timer(self, event: TimerEvent) -> None:
        if event.name == "rollover":
            charge = self.account.charge_financing(event.ts, event.multiplier)
            self.output.financing.append((event.ts, event.multiplier, charge))
        elif event.name == "day_end":
            day = date.fromisoformat(str(event.ref))
            account = self.account.state(event.ts)
            self.risk_state.close_day(account)
            self.recorder.account(account, "day_end")
            self.output.day_states.append(DaySnapshot(day, account))
        elif event.name == "expire":
            self.broker.expire(str(event.ref), event.ts)
        elif event.name == "time_stop":
            self._exit(event, "time stop")
        else:
            self._exit(event, "flat before the weekly close")

    def _exit(self, event: TimerEvent, reason: str) -> None:
        if self.broker.position_lots == 0:
            return
        if event.name == "time_stop" and event.ref != self._position_intent:
            return  # the position has changed since; that intent's time stop no longer applies
        now = np.array([event.ts], dtype=np.int64)
        if not bool(self.market_clock.is_open(now)[0]):
            reopen = int(self.market_clock.advance(now, 0)[0])
            if reopen != NAT_NS:
                self.queue.push(TimerEvent(reopen, event.name, ref=event.ref))
            return
        self._push_intent(
            TradeIntent(direction="flat", strategy_id=self.strategy.strategy_id, reason=reason)
        )

    def _context(self) -> StrategyContext:
        now = self.clock.now
        return StrategyContext(now, self.account.state(now), self.last_quote, self.sigma.at(now))

    def _market(self, now: int) -> MarketState | None:
        quote = self.last_quote
        if quote is None:
            return None
        return MarketState(
            ts=now,
            bid=quote.bid,
            ask=quote.ask,
            quote_ts=quote.ts,
            sigma_daily=self.sigma.at(now),
            sessions=self._sessions(now),
        )

    def _sessions(self, now: int) -> tuple[str, ...]:
        """The sessions the risk profile caps that are in force at `now`."""
        names = self.risk.watched_sessions
        if not names:
            return ()
        day = trading_day(from_ns(now))
        windows = self._session_windows.get(day)
        if windows is None:
            row = build_session_table(self.costs.sessions, day, day).iloc[0]
            windows = {}
            for name in names:
                opens, closes = row[f"{name}_open_utc"], row[f"{name}_close_utc"]
                if not (pd.isna(opens) or pd.isna(closes)):
                    windows[name] = (pd.Timestamp(opens).value, pd.Timestamp(closes).value)
            self._session_windows[day] = windows
        return tuple(name for name, (o, c) in windows.items() if o <= now < c)


# --- running a backtest -------------------------------------------------------------------------


@dataclass(frozen=True)
class EventBacktestResult(BacktestResult):
    """An event backtest: the screener's result layout plus the event tier's records.

    `fills`, `daily`, `trades` (FIFO) and `financing` share the screener's columns where they mean
    the same, so the BT-003 metrics apply to both tiers. `missed` are the decision times of orders
    that expired unfilled and `closed` those refused because the market was closed. `ledger` is the
    decision ledger (BT-007) and `link_problems` its broken links (empty when every order is backed
    by an approved risk decision). `equity` has the account at every signal bar; `ambiguity`
    reports how often a bar touched both legs of a bracket (resolved by ticks in tick mode, by the
    pessimistic rule in bar mode). `risk_label` names the risk engine and its profile version.
    """

    ledger: pd.DataFrame
    ledger_summary: pd.DataFrame
    link_problems: tuple[str, ...]
    equity: pd.DataFrame
    brackets: pd.DataFrame
    ambiguity: dict[str, float | str]
    refusals: pd.DataFrame
    mode: str
    risk_label: str
    strategy_id: str
    strategy_version: str
    events: int


def run_event_backtest(
    strategy: Strategy,
    data: MarketData,
    costs: CostModel,
    clock: MarketClock,
    *,
    capital: float,
    margin_rate: float,
    risk: RiskEngine,
    constraints: Constraints | None = None,
    sigma_1m_bps: pd.Series | None = None,
    sigma_daily: pd.Series | None = None,
    observer: Callable[[Event, int], None] | None = None,
) -> EventBacktestResult:
    """Run `strategy` on `data` through the simulated broker, portfolio and ledger.

    Args:
        strategy: The strategy (it only emits intents).
        data: Tick-mode or bar-mode market data.
        costs: The cost model (fills, slippage, commission, financing, latency).
        clock: Market clock covering the data's trading days and a week after them.
        capital: Starting capital (USD).
        margin_rate: Margin per unit of notional (`backtest.event.margin_rate`).
        risk: The risk engine (RISK-005); its margin rate should be `margin_rate`.
        constraints: Session constraints (BT-008); default none beyond the closed-market rule.
        sigma_1m_bps: Sigma-hat of one-minute returns in bps, known at each instant (slippage).
        sigma_daily: Daily sigma-hat (fraction of price) indexed by when each value is known,
            for stops and sizing; default the interim EWMA of the signal bars.
        observer: Called after every event with the event and the clock.
    """
    portfolio = Portfolio(costs, capital=capital, margin_rate=margin_rate)
    ledger = Ledger()
    rules: Constraints = constraints if constraints is not None else NoConstraints()
    broker = SimulatedBroker(
        costs,
        clock,
        recorder=ledger,
        margin_rate=margin_rate,
        equity=lambda ts: portfolio.equity,
        entry_blackout=rules.entry_blackout,
        sigma_1m_bps=sigma_1m_bps,
    )
    engine = EventEngine(
        strategy,
        data,
        broker=broker,
        risk=risk,
        account=portfolio,
        recorder=ledger,
        costs=costs,
        clock=clock,
        constraints=rules,
        sigma=None if sigma_daily is None else SeriesSigma(sigma_daily),
        observer=observer,
    )
    output = engine.run()
    fills = fills_frame(output.fills)
    financing = portfolio.financing_series()
    contract = float(costs.instrument.contract_size)
    daily = daily_frame(
        [(snapshot.day, snapshot.state) for snapshot in output.day_states],
        fills,
        financing,
        capital=capital,
        contract=contract,
    )
    frame = ledger.frame()
    refusals = pd.DataFrame(
        output.refused, columns=["decision_time", "intent_id", "reason"]
    ).astype({"decision_time": "int64"})
    refusals["decision_time"] = pd.to_datetime(refusals["decision_time"], unit="ns", utc=True)
    closed = refusals.loc[refusals["reason"].str.startswith("market closed"), "decision_time"]
    return EventBacktestResult(
        fills=fills,
        missed=_expired_decisions(frame),
        closed=pd.DatetimeIndex(closed),
        daily=daily,
        trades=portfolio.trades_frame(),
        financing=financing,
        capital=float(capital),
        contract_size=contract,
        cost_basis=costs.result_label,
        ledger=frame,
        ledger_summary=ledger.summary(),
        link_problems=tuple(ledger.check_links()),
        equity=_equity_frame(output.bar_states),
        brackets=_brackets_frame(broker.brackets),
        ambiguity=_ambiguity(broker, data),
        refusals=refusals,
        mode=data.mode,
        risk_label=risk.label,
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version,
        events=output.events,
    )


def _expired_decisions(ledger: pd.DataFrame) -> pd.DatetimeIndex:
    expired = ledger.loc[ledger["kind"] == "order_expired", "intent_id"]
    intents = ledger.loc[ledger["kind"] == "intent"].set_index("intent_id")["ts"]
    times = [intents[i] for i in expired if i in intents.index]
    return pd.DatetimeIndex(times, tz="UTC") if times else pd.DatetimeIndex([], tz="UTC")


def _equity_frame(states: list[AccountState]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "time": pd.to_datetime([s.ts for s in states], unit="ns", utc=True),
            "cash": [s.cash for s in states],
            "unrealized": [s.unrealized for s in states],
            "equity": [s.equity for s in states],
            "position_lots": [s.position_lots for s in states],
            "margin_used": [s.margin_used for s in states],
            "mark": [s.mark for s in states],
        }
    )
    return frame


def _brackets_frame(brackets: list[BracketRecord]) -> pd.DataFrame:
    columns = [
        "parent_order_id",
        "side",
        "stop",
        "target",
        "activated_at",
        "closed_at",
        "exit_role",
        "ambiguous",
    ]
    rows = [
        {
            "parent_order_id": b.parent_order_id,
            "side": b.side.value,
            "stop": b.stop,
            "target": b.target,
            "activated_at": from_ns(b.activated_at),
            "closed_at": pd.NaT if b.closed_at is None else from_ns(b.closed_at),
            "exit_role": b.exit_role,
            "ambiguous": b.ambiguous,
        }
        for b in brackets
    ]
    return pd.DataFrame(rows, columns=columns)


def _ambiguity(broker: SimulatedBroker, data: MarketData) -> dict[str, float | str]:
    """How often one bar touched both legs of a bracket (the share the plan asks every report for).

    Bar mode: the broker's own count, each resolved to the stop loss. Tick mode: the one-minute
    bars in which a bracket ended and whose range reached its stop and went a tick through its
    target — bars alone could not have told which came first; the ticks did.
    """
    exits = sum(b.exit_role in ("stop_loss", "take_profit") for b in broker.brackets)
    if data.mode == "bars":
        bars, ambiguous = broker.bracket_bars, broker.ambiguous_bars
        resolution = "pessimistic: the stop loss is assumed first"
    else:
        minute = data.minute_bars
        starts = minute["bar_start_utc"].to_numpy(np.int64)
        bars = ambiguous = 0
        for bracket in broker.brackets:
            end = bracket.closed_at if bracket.closed_at is not None else data.end
            first = max(int(np.searchsorted(starts, bracket.activated_at, side="right")) - 1, 0)
            last = int(np.searchsorted(starts, end, side="right")) - 1
            if last < first:
                continue
            bars += last - first + 1
            if bracket.exit_role is None or bracket.stop is None or bracket.target is None:
                continue
            row = minute.iloc[last]
            if bracket.side is Side.BUY:  # a long: sell legs against the bid
                both = row["bid_low"] <= bracket.stop and through(
                    row["bid_high"], bracket.target, broker.tick, above=True
                )
            else:
                both = row["ask_high"] >= bracket.stop and through(
                    row["ask_low"], bracket.target, broker.tick, above=False
                )
            ambiguous += int(both)
        resolution = "ticks"
    return {
        "resolution": resolution,
        "bracket_bars": float(bars),
        "ambiguous_bars": float(ambiguous),
        "ambiguous_share": ambiguous / bars if bars else float("nan"),
        "bracket_exits": float(exits),
    }
