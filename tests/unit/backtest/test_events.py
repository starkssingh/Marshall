"""BT-004: event queue and clock, the market-data stream, the strategy interface, the decision
schemas and the placeholder risk approver, and the engine loop's ordering and determinism."""

from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from helpers.pipeline import REPO
from xq.backtest.costs import CostModel
from xq.backtest.engine import EventEngine, MarketData, Strategy, StrategyContext
from xq.backtest.events import (
    AccountState,
    Bar,
    BarEvent,
    Event,
    EventQueue,
    ExecutionBarEvent,
    Fill,
    FillEvent,
    OrderEvent,
    Quote,
    Rank,
    SignalEvent,
    SimulationClock,
    TickEvent,
    TimerEvent,
    clean_lots,
)
from xq.core.config import load_config
from xq.core.errors import NaiveTimestampError
from xq.core.time import to_ns
from xq.core.types import Side, Timeframe
from xq.data.calendar import MarketClock
from xq.risk.placeholder import (
    PLACEHOLDER_CONFIG_VERSION,
    PLACEHOLDER_REASON,
    PassThroughRiskApprover,
)
from xq.signals.schema import OrderIntent, RiskDecision, TradeIntent

CFG = load_config("research", config_dir=REPO / "config")
CLOCK = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 1), date(2024, 3, 31))
COSTS = CostModel.from_config(CFG, "xauusd")
INSTRUMENT = CFG.instrument("xauusd")


def ns(text: str) -> int:
    return to_ns(pd.Timestamp(text, tz="UTC"))


def quotes(*rows: tuple[str, float, float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": [pd.Timestamp(t, tz="UTC") for t, _, _ in rows],
            "bid": [b for _, b, _ in rows],
            "ask": [a for _, _, a in rows],
        }
    )


def minute_quotes(start: str, end: str, *, price: float = 2000.0, seed: int = 3) -> pd.DataFrame:
    """A quote every 20 seconds in ``[start, end)`` (market hours only), random-walk mids."""
    times = pd.date_range(start, end, freq="20s", tz="UTC", inclusive="left")
    t = times.as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
    times = times[CLOCK.is_open(t)]
    rng = np.random.default_rng(seed)
    mid = price + np.cumsum(rng.normal(0, 0.1, len(times)))
    return pd.DataFrame({"ts_utc": times, "bid": mid - 0.1, "ask": mid + 0.1})


def state(position: float = 0.0) -> AccountState:
    return AccountState(0, 100_000.0, 100_000.0, 0.0, 100_000.0, position, 0.0)


def stamped(intent: TradeIntent, n: int = 1, at: str = "2024-03-12 14:00") -> TradeIntent:
    return intent.model_copy(
        update={"intent_id": f"I{n:06d}", "created_at": pd.Timestamp(at, tz="UTC")}
    )


# --- queue and clock ----------------------------------------------------------------------------


def test_the_queue_orders_by_time_then_rank_then_insertion() -> None:
    queue = EventQueue()
    t = ns("2024-03-12 14:00")
    intent = stamped(TradeIntent(direction="flat"))
    bar = Bar(t - 900_000_000_000, t, t, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    pushed: list[Event] = [
        SignalEvent(t, intent),
        BarEvent(t, bar),
        TickEvent(t, 1.0, 1.1),
        TimerEvent(t + 1, "day_end"),
        OrderEvent(t, "O-1"),
        TimerEvent(t, "rollover"),
        TickEvent(t, 2.0, 2.1),  # same instant and rank as the first tick: after it
        TimerEvent(t - 1, "expire", ref="O-0"),
    ]
    for event in pushed:
        queue.push(event)
    popped = [queue.pop() for _ in range(len(pushed))]
    assert [(e.ts - t, int(e.rank)) for e in popped] == [
        (-1, Rank.TIMER),
        (0, Rank.TIMER),
        (0, Rank.ORDER),
        (0, Rank.MARKET),
        (0, Rank.MARKET),
        (0, Rank.BAR),
        (0, Rank.SIGNAL),
        (1, Rank.TIMER),
    ]
    ticks = [e for e in popped if isinstance(e, TickEvent)]
    assert [tick.bid for tick in ticks] == [1.0, 2.0]
    assert len(queue) == 0
    assert queue.peek_key() is None
    with pytest.raises(IndexError):
        queue.pop()


def test_ranks_put_orders_before_quotes_and_fills_before_decisions() -> None:
    # an order arriving at t can fill on a quote at t; a fill at t is booked before a decision at t
    assert Rank.TIMER < Rank.ORDER < Rank.MARKET < Rank.FILL < Rank.BAR < Rank.SIGNAL
    assert TimerEvent.rank is Rank.TIMER
    assert FillEvent.rank is Rank.FILL
    assert ExecutionBarEvent.rank is Rank.MARKET


def test_the_simulation_clock_never_goes_back() -> None:
    clock = SimulationClock(100)
    clock.advance_to(100)
    clock.advance_to(250)
    assert clock.now == 250
    with pytest.raises(ValueError, match="back in time"):
        clock.advance_to(249)


def test_clean_lots_removes_float_dust_and_negative_zero() -> None:
    assert clean_lots(0.1 + 0.2 - 0.3) == 0.0
    assert str(clean_lots(-0.0)) == "0.0"
    assert clean_lots(0.49 + 0.01) == 0.5


# --- market data --------------------------------------------------------------------------------


def test_tick_data_builds_bars_available_at_their_end_plus_latency() -> None:
    q = minute_quotes("2024-03-12 14:00", "2024-03-12 15:00")
    data = MarketData.from_ticks(q, Timeframe.M15, publication_latency_ms=250)
    assert data.mode == "ticks"
    bars = data.bars
    assert len(bars) == 4
    start = bars["bar_start_utc"].to_numpy(np.int64)
    np.testing.assert_array_equal(
        bars["available_at_utc"].to_numpy(np.int64), start + Timeframe.M15.nanos + 250_000_000
    )
    assert len(data.minute_bars) == 60
    assert data.trading_days() == [date(2024, 3, 12)]


def test_the_stream_is_sorted_and_puts_a_quote_at_a_bars_availability_first() -> None:
    q = minute_quotes("2024-03-12 14:00", "2024-03-12 14:31")  # a quote at exactly 14:15:00
    data = MarketData.from_ticks(q, Timeframe.M15)
    events = list(data.events())
    keys = [(e.ts, int(e.rank)) for e in events]
    assert keys == sorted(keys)
    at = ns("2024-03-12 14:15")
    same_instant = [e for e in events if e.ts == at]
    assert [type(e) for e in same_instant] == [TickEvent, BarEvent]
    bar_events = [e for e in events if isinstance(e, BarEvent)]
    assert [e.bar.end for e in bar_events] == [e.ts for e in bar_events]  # no latency
    first = bar_events[0].bar
    ticks = q.loc[(q["ts_utc"] >= "2024-03-12 14:00Z") & (q["ts_utc"] < "2024-03-12 14:15Z")]
    assert first.open == pytest.approx((ticks["bid"].iloc[0] + ticks["ask"].iloc[0]) / 2)
    assert first.close == pytest.approx((ticks["bid"].iloc[-1] + ticks["ask"].iloc[-1]) / 2)
    assert first.bid_close == ticks["bid"].iloc[-1]
    assert list(data.events()) == events  # deterministic


def test_bar_mode_streams_each_execution_bar_as_open_then_range() -> None:
    ticks = MarketData.from_ticks(
        minute_quotes("2024-03-12 14:00", "2024-03-12 14:30"), Timeframe.M15
    )
    data = MarketData.from_bars(ticks.bars, Timeframe.M15, execution_bars=ticks.minute_bars)
    assert data.mode == "bars"
    events = list(data.events())
    execution = [e for e in events if isinstance(e, ExecutionBarEvent)]
    assert len(execution) == 2 * 30
    first, second = execution[0], execution[1]
    assert (first.phase, second.phase) == ("open", "range")
    assert first.ts == first.bar.start
    assert second.ts == first.bar.end - 1
    keys = [(e.ts, int(e.rank)) for e in events]
    assert keys == sorted(keys)


def test_market_data_is_validated() -> None:
    naive = pd.DataFrame(
        {"ts_utc": pd.to_datetime(["2024-03-12 14:00"]), "bid": [1.0], "ask": [1.1]}
    )
    with pytest.raises(NaiveTimestampError):
        MarketData.from_ticks(naive, Timeframe.M15)
    crossed = quotes(("2024-03-12 14:00", 2000.2, 2000.0))
    with pytest.raises(ValueError, match="crossed"):
        MarketData.from_ticks(crossed, Timeframe.M15)
    unordered = quotes(("2024-03-12 14:01", 2000.0, 2000.2), ("2024-03-12 14:00", 2000.0, 2000.2))
    with pytest.raises(ValueError, match="time order"):
        MarketData.from_ticks(unordered, Timeframe.M15)


# --- schemas and the placeholder approver -------------------------------------------------------


def test_trade_intents_are_validated() -> None:
    TradeIntent(direction="long", exposure=1.0, stop=1990.0, target=2020.0)
    TradeIntent(direction="short", exposure=1.0, stop=2020.0, target=1990.0)
    with pytest.raises(ValidationError, match="wrong sides"):
        TradeIntent(direction="long", exposure=1.0, stop=2020.0, target=1990.0)
    with pytest.raises(ValidationError, match="positive exposure"):
        TradeIntent(direction="long")
    with pytest.raises(ValidationError, match="flat intent"):
        TradeIntent(direction="flat", stop=1990.0)
    with pytest.raises(ValidationError, match="need limit_price"):
        TradeIntent(direction="long", exposure=1.0, entry_type="limit")


def test_an_order_can_only_be_built_from_an_approved_decision() -> None:
    intent = stamped(TradeIntent(direction="long", exposure=1.0, stop=1990.0))
    approver = PassThroughRiskApprover(INSTRUMENT, 100_000.0)
    decision = approver.evaluate(intent, state(), Quote(0, 1999.9, 2000.1))
    order = OrderIntent.from_decision(decision, intent, expected_position_lots=0.0)
    assert (order.side, order.size_lots, order.stop) == (Side.BUY, 0.5, 1990.0)
    assert order.decision_id == decision.decision_id == "D-I000001"
    assert order.order_id == "O-I000001"
    assert order.signed_lots == 0.5
    rejected = RiskDecision(
        decision_id="D-x",
        intent_id="I000001",
        decided_at=pd.Timestamp("2024-03-12 14:00", tz="UTC"),
        approved=False,
        reasons=("limit",),
        config_version="test",
    )
    with pytest.raises(ValueError, match="needs no order"):
        OrderIntent.from_decision(rejected, intent, expected_position_lots=0.0)
    with pytest.raises(ValidationError, match="approved risk decision"):
        OrderIntent(
            decision=rejected,
            order_id="O-x",
            side=Side.BUY,
            size_lots=0.5,
            order_type="market",
            expected_position_lots=0.0,
            idempotency_key="O-x",
        )
    with pytest.raises(ValidationError, match="side and size"):
        OrderIntent(
            decision=decision,
            order_id="O-x",
            side=Side.BUY,
            size_lots=5.0,  # more than risk approved
            order_type="market",
            stop=1990.0,
            expected_position_lots=0.0,
            idempotency_key="O-x",
        )
    with pytest.raises(ValidationError, match="rejected decision"):
        RiskDecision(
            decision_id="D-y",
            intent_id="I1",
            decided_at=pd.Timestamp("2024-03-12", tz="UTC"),
            approved=False,
            side=Side.BUY,
            size_lots=1.0,
            reasons=("x",),
            config_version="test",
        )


def test_the_placeholder_approves_everything_and_says_so() -> None:
    approver = PassThroughRiskApprover(INSTRUMENT, 100_000.0)
    quote = Quote(0, 2000.9, 2001.1)  # mid 2001
    # 0.7 * 100,000 / (2001 * 100 oz) = 0.34983 lots, rounded down to the 0.01 lot step
    long = approver.evaluate(stamped(TradeIntent(direction="long", exposure=0.7)), state(), quote)
    assert long.approved
    assert long.side is Side.BUY
    assert (long.size_lots, long.target_lots) == (0.34, 0.34)
    assert long.reasons[0] == PLACEHOLDER_REASON
    assert "PLACEHOLDER" in long.reasons[0]
    assert long.config_version == PLACEHOLDER_CONFIG_VERSION
    assert long.limits_snapshot["requested_lots"] == pytest.approx(0.7 * 1000 / 2001)
    flip = approver.evaluate(
        stamped(TradeIntent(direction="short", exposure=0.5)), state(0.34), quote
    )
    assert (flip.side, flip.size_lots, flip.target_lots) == (Side.SELL, 0.58, -0.24)
    flat = approver.evaluate(stamped(TradeIntent(direction="flat")), state(-0.24), quote)
    assert (flat.side, flat.size_lots, flat.target_lots) == (Side.BUY, 0.24, 0.0)
    same = approver.evaluate(
        stamped(TradeIntent(direction="long", exposure=0.7)), state(0.34), quote
    )
    assert same.approved
    assert same.side is None
    assert same.size_lots == 0
    assert "unchanged" in same.reasons[1]
    tiny = approver.evaluate(stamped(TradeIntent(direction="long", exposure=0.001)), state(), quote)
    assert tiny.side is None  # below the minimum lot: nothing to trade
    blind = approver.evaluate(stamped(TradeIntent(direction="long", exposure=1.0)), state(), None)
    assert not blind.approved
    assert "no quote" in blind.reasons[1]
    with pytest.raises(ValueError, match="stamps intent_id"):
        approver.evaluate(TradeIntent(direction="flat"), state(), quote)


# --- the engine loop with stub components -------------------------------------------------------


class StubBroker:
    """Accepts orders and never fills them; records what it saw."""

    def __init__(self) -> None:
        self.position_lots = 0.0
        self.submitted: list[tuple[int, str]] = []
        self.arrived: list[tuple[int, str]] = []

    def submit(self, order: OrderIntent, now: int) -> int | None:
        self.submitted.append((now, order.order_id))
        return int(CLOCK.advance(np.array([now], dtype=np.int64), COSTS.latency.value)[0])

    def arrive(self, order_id: str, now: int) -> int | None:
        self.arrived.append((now, order_id))
        return None

    def on_market(self, event: TickEvent | ExecutionBarEvent) -> list[Fill]:
        return []

    def expire(self, order_id: str, now: int) -> None:
        pass

    def cancel_pending_entries(self, now: int, reason: str) -> None:
        pass

    def finish(self, now: int) -> None:
        pass


class StubAccount:
    def __init__(self) -> None:
        self.marks: list[int] = []
        self.financing: list[tuple[int, int]] = []

    def mark(self, quote: Quote) -> None:
        self.marks.append(quote.ts)

    def book(self, fill: Fill) -> None:
        raise AssertionError("the stub broker never fills")

    def charge_financing(self, ts: int, multiplier: int) -> float:
        self.financing.append((ts, multiplier))
        return 0.0

    def state(self, ts: int) -> AccountState:
        return AccountState(ts, 100_000.0, 100_000.0, 0.0, 100_000.0, 0.0, 0.0)


class StubRecorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def intent(self, intent: TradeIntent) -> None:
        self.rows.append(("intent", str(intent.intent_id)))

    def refusal(self, intent: TradeIntent, reason: str) -> None:
        self.rows.append(("refusal", reason))

    def decision(self, decision: RiskDecision) -> None:
        self.rows.append(("decision", decision.decision_id))

    def order(self, order: OrderIntent, *, submitted_at: int, arrival: int | None) -> None:
        self.rows.append(("order", order.order_id))

    def fill(self, fill: Fill) -> None:
        self.rows.append(("fill", fill.fill_id))


class LongEveryBar(Strategy):
    strategy_id = "long_every_bar"

    def __init__(self) -> None:
        self.seen: list[tuple[int, int]] = []

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[TradeIntent]:
        self.seen.append((bar.available_at, ctx.now))
        return [TradeIntent(direction="long", exposure=1.0, strategy_id=self.strategy_id)]


def run_stub(data: MarketData) -> tuple[EventEngine, list[tuple[int, int, str]]]:
    trace: list[tuple[int, int, str]] = []
    engine = EventEngine(
        LongEveryBar(),
        data,
        broker=StubBroker(),
        risk=PassThroughRiskApprover(INSTRUMENT, 100_000.0),
        account=StubAccount(),
        recorder=StubRecorder(),
        costs=COSTS,
        clock=CLOCK,
        observer=lambda event, now: trace.append((now, int(event.rank), type(event).__name__)),
    )
    engine.run()
    return engine, trace


def test_the_engine_decides_at_bar_availability_and_orders_after_the_latency() -> None:
    # Tuesday 20:00-22:30 UTC: the 17:00 New York close (21:00 UTC) and the 22:00 reopen inside
    q = minute_quotes("2024-03-12 20:00", "2024-03-12 22:30")
    engine, trace = run_stub(MarketData.from_ticks(q, Timeframe.M15))
    strategy = engine.strategy
    assert isinstance(strategy, LongEveryBar)
    assert all(available == now for available, now in strategy.seen)  # decision time = available_at
    times = [now for now, _, _ in trace]
    assert times == sorted(times)
    recorder = engine.recorder
    assert isinstance(recorder, StubRecorder)
    refusals = [reason for kind, reason in recorder.rows if kind == "refusal"]
    # the bar ending at 21:00 is decided at the close itself: no order (ADR 0032)
    assert refusals == ["market closed at the decision time: no order (ADR 0032)"]
    broker = engine.broker
    assert isinstance(broker, StubBroker)
    assert len(broker.submitted) == len(strategy.seen) - 1
    for (sent, order_id), (arrived, arrived_id) in zip(
        broker.submitted, broker.arrived, strict=True
    ):
        assert order_id == arrived_id
        assert arrived == sent + COSTS.latency.value  # one second of market time
    ids = [value for kind, value in recorder.rows if kind == "intent"]
    assert ids == [f"I{n:06d}" for n in range(1, len(ids) + 1)]
    account = engine.account
    assert isinstance(account, StubAccount)
    # Tuesday's rollover (x1) and, the data reaching Wednesday's trading day, Wednesday's (x3)
    assert account.financing == [(ns("2024-03-12 21:00"), 1), (ns("2024-03-13 21:00"), 3)]
    assert [s.day for s in engine.output.day_states] == [date(2024, 3, 12), date(2024, 3, 13)]


def test_signals_follow_bars_and_timers_precede_quotes_at_one_instant() -> None:
    q = minute_quotes("2024-03-12 20:00", "2024-03-12 21:00")
    _, trace = run_stub(MarketData.from_ticks(q, Timeframe.M15))
    at = ns("2024-03-12 20:15")
    kinds = [kind for now, _, kind in trace if now == at]
    assert kinds == ["TickEvent", "BarEvent", "SignalEvent"]
    close = ns("2024-03-12 21:00")
    kinds = [kind for now, _, kind in trace if now == close]
    assert kinds[:2] == ["TimerEvent", "TimerEvent"]  # the rollover, then Tuesday's end


def test_two_runs_give_the_same_trace() -> None:
    q = minute_quotes("2024-03-12 13:00", "2024-03-12 23:00", seed=11)
    data = MarketData.from_ticks(q, Timeframe.M15)
    first, second = run_stub(data)[1], run_stub(data)[1]
    assert first == second
    assert len(first) > 1000


def test_an_engine_runs_once() -> None:
    engine, _ = run_stub(
        MarketData.from_ticks(minute_quotes("2024-03-12 14:00", "2024-03-12 14:30"), Timeframe.M15)
    )
    with pytest.raises(RuntimeError, match="runs once"):
        engine.run()
