"""BT-005: the broker simulator on hand-computed cases — next-quote fills on the correct side, stops
gapped through, SL and TP in one bar (pessimistic in bar mode, resolved by ticks), no fills while
the market is closed, expiry, limit and stop entries, margin and stale-position rejections."""

from datetime import date

import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.backtest.broker_sim import SimulatedBroker
from xq.backtest.costs import CostModel
from xq.backtest.events import ExecutionBar, ExecutionBarEvent, Fill, TickEvent
from xq.core.config import CostModelConfig, load_config
from xq.core.time import to_ns
from xq.core.types import Side
from xq.data.calendar import MarketClock
from xq.signals.schema import OrderIntent, RiskDecision

CFG = load_config("research", config_dir=REPO / "config")
CLOCK = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 1), date(2024, 3, 31))
# 3.5 USD per lot per side, 0.5 bp slippage (no multipliers), 1 s latency, 300 s fill delay
COSTS = CostModel(
    CostModelConfig(
        venue="test",
        provisional=True,
        latency_ms=1000,
        max_fill_delay_s=300,
        commission={"per_lot_per_side_usd": 3.5},  # type: ignore[arg-type]
        slippage={"fixed_bps": 0.5, "sigma_multiple": 0.0},  # type: ignore[arg-type]
        financing={"long_rate_annual_pct": 3.6, "short_rate_annual_pct": 3.6},  # type: ignore[arg-type]
    ),
    CFG.instrument("xauusd"),
    CFG.sessions_config(),
)
SLIP = 0.5e-4
MINUTE = 60_000_000_000


def ns(text: str) -> int:
    return to_ns(pd.Timestamp(text, tz="UTC"))


class Recorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def fill(self, fill: Fill) -> None:
        self.rows.append(("fill", fill.order_id, fill.role))

    def order_rejected(self, order_id: str, ts: int, reason: str) -> None:
        self.rows.append(("rejected", order_id, reason))

    def order_cancelled(self, order_id: str, ts: int, reason: str) -> None:
        self.rows.append(("cancelled", order_id, reason))

    def order_expired(self, order_id: str, ts: int, reason: str) -> None:
        self.rows.append(("expired", order_id, reason))

    def bracket_order(self, **fields: object) -> None:
        self.rows.append(("bracket", str(fields["order_id"]), str(fields["decision_id"])))


def make_broker(equity: float = 100_000.0) -> tuple[SimulatedBroker, Recorder]:
    recorder = Recorder()
    broker = SimulatedBroker(
        COSTS, CLOCK, recorder=recorder, margin_rate=0.05, equity=lambda ts: equity
    )
    return broker, recorder


def order(
    side: Side,
    lots: float,
    *,
    n: int = 1,
    kind: str = "market",
    price: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    expected: float = 0.0,
) -> OrderIntent:
    decision = RiskDecision(
        decision_id=f"D-I{n:06d}",
        intent_id=f"I{n:06d}",
        decided_at=pd.Timestamp("2024-03-12 14:00", tz="UTC"),
        approved=True,
        side=side,
        size_lots=lots,
        adjusted_stop=stop,
        target=target,
        reasons=("test",),
        config_version="test",
    )
    return OrderIntent(
        decision=decision,
        order_id=f"O-I{n:06d}",
        side=side,
        size_lots=lots,
        order_type=kind,  # type: ignore[arg-type]
        price=price,
        stop=stop,
        target=target,
        expected_position_lots=expected,
        idempotency_key=f"O-I{n:06d}",
    )


def send(broker: SimulatedBroker, o: OrderIntent, at: str) -> tuple[int | None, int | None]:
    arrival = broker.submit(o, ns(at))
    assert arrival is not None
    return arrival, broker.arrive(o.order_id, arrival)


def tick(broker: SimulatedBroker, at: str, bid: float, ask: float) -> list[Fill]:
    return broker.on_market(TickEvent(ns(at), bid, ask))


def minute_bar(
    at: str, bid: tuple[float, float, float, float], spread: float = 0.2
) -> ExecutionBar:
    o, h, lo, c = bid
    start = ns(at)
    return ExecutionBar(
        start, start + MINUTE, o, h, lo, c, o + spread, h + spread, lo + spread, c + spread, spread
    )


def run_bar(broker: SimulatedBroker, bar: ExecutionBar) -> list[Fill]:
    fills = broker.on_market(ExecutionBarEvent(bar.start, bar, "open"))
    return fills + broker.on_market(ExecutionBarEvent(bar.end - 1, bar, "range"))


def long_with_bracket(broker: SimulatedBroker) -> Fill:
    """Buy 0.5 lots at 14:00 with a stop at 1995 and a target at 2010; filled at 14:00:02."""
    tick(broker, "2024-03-12 14:00:00", 1999.9, 2000.1)
    send(broker, order(Side.BUY, 0.5, stop=1995.0, target=2010.0), "2024-03-12 14:00")
    [fill] = tick(broker, "2024-03-12 14:00:02", 1999.9, 2000.1)
    return fill


# --- market orders ------------------------------------------------------------------------------


def test_a_market_buy_fills_at_the_next_ask_plus_slippage() -> None:
    broker, _ = make_broker()
    tick(broker, "2024-03-12 14:00:00", 1999.8, 2000.0)
    arrival, expiry = send(broker, order(Side.BUY, 0.5), "2024-03-12 14:00")
    assert arrival == ns("2024-03-12 14:00:01")  # one second of market time
    assert expiry == arrival + 300_000_000_000 + 1
    assert tick(broker, "2024-03-12 14:00:00.5", 1999.8, 2000.0) == []  # before arrival
    [fill] = tick(broker, "2024-03-12 14:00:02", 1999.9, 2000.1)
    assert fill.ts == ns("2024-03-12 14:00:02")
    assert fill.price == pytest.approx(2000.1 * (1 + SLIP))  # the ask, never the mid
    assert fill.lots == 0.5
    assert fill.mid == pytest.approx(2000.0)
    assert fill.spread_cost == pytest.approx(0.5 * 0.1 * 100)  # half-spread on 50 oz
    assert fill.slippage_cost == pytest.approx(0.5 * 2000.1 * SLIP * 100)
    assert fill.commission == pytest.approx(0.5 * 3.5)
    assert fill.lots * (fill.price - fill.mid) * 100 == pytest.approx(
        fill.spread_cost + fill.slippage_cost
    )
    assert (fill.role, fill.position_after, broker.position_lots) == ("entry", 0.5, 0.5)
    assert fill.fill_id == "F-O-I000001"
    assert fill.decision_id == "D-I000001"


def test_a_market_sell_fills_at_the_bid_minus_slippage() -> None:
    broker, _ = make_broker()
    tick(broker, "2024-03-12 14:00:00", 1999.9, 2000.1)
    send(broker, order(Side.SELL, 0.3), "2024-03-12 14:00")
    [fill] = tick(broker, "2024-03-12 14:00:01", 2001.0, 2001.4)  # exactly at arrival
    assert fill.price == pytest.approx(2001.0 * (1 - SLIP))
    assert fill.lots == -0.3
    assert fill.slippage_cost == pytest.approx(0.3 * 2001.0 * SLIP * 100)


def test_a_market_order_without_a_timely_quote_expires() -> None:
    broker, recorder = make_broker()
    tick(broker, "2024-03-12 14:00:00", 1999.9, 2000.1)
    _, expiry = send(broker, order(Side.BUY, 0.5), "2024-03-12 14:00")
    assert expiry is not None
    broker.expire("O-I000001", expiry)  # the engine's timer
    assert tick(broker, "2024-03-12 14:10:00", 1999.9, 2000.1) == []
    assert recorder.rows == [("expired", "O-I000001", "no quote within 300 s of arrival")]
    assert broker.position_lots == 0


def test_a_late_quote_does_not_fill_even_without_the_timer() -> None:
    broker, recorder = make_broker()
    tick(broker, "2024-03-12 14:00:00", 1999.9, 2000.1)
    send(broker, order(Side.BUY, 0.5), "2024-03-12 14:00")
    assert tick(broker, "2024-03-12 14:05:01.000000001", 1999.9, 2000.1) == []
    assert recorder.rows[0][0] == "expired"


# --- stops, targets and brackets ----------------------------------------------------------------


def test_a_stop_gapped_through_fills_at_the_first_price_beyond_it() -> None:
    broker, recorder = make_broker()
    entry = long_with_bracket(broker)
    assert entry.position_after == 0.5
    assert [r[1] for r in recorder.rows if r[0] == "bracket"] == ["O-I000001-SL", "O-I000001-TP"]
    assert tick(broker, "2024-03-12 14:01", 1998.0, 1998.2) == []
    [stop] = tick(broker, "2024-03-12 14:02", 1990.0, 1990.2)  # jumps from 1998 through 1995
    assert stop.price == pytest.approx(1990.0 * (1 - SLIP))  # not the stop price
    assert (stop.role, stop.order_type, stop.lots) == ("stop_loss", "stop", -0.5)
    assert stop.decision_id == "D-I000001"  # the legs belong to the entry's decision
    assert broker.position_lots == 0
    assert ("cancelled", "O-I000001-TP", "OCO: O-I000001-SL filled") in recorder.rows
    assert broker.brackets[0].exit_role == "stop_loss"


def test_a_target_fills_at_its_limit_price_and_cancels_the_stop() -> None:
    broker, recorder = make_broker()
    long_with_bracket(broker)
    [target] = tick(broker, "2024-03-12 14:03", 2012.0, 2012.2)
    assert target.price == 2010.0  # a limit never fills better than its price
    assert target.role == "take_profit"
    assert target.slippage_bps == 0.0
    assert ("cancelled", "O-I000001-SL", "OCO: O-I000001-TP filled") in recorder.rows


def test_bracket_legs_start_after_the_entrys_own_quote() -> None:
    broker, _ = make_broker()
    tick(broker, "2024-03-12 14:00:00", 1999.9, 2000.1)
    # a stop inside the spread would trigger on the entry's quote if the legs were active on it
    send(broker, order(Side.BUY, 0.5, stop=1999.95), "2024-03-12 14:00")
    fills = tick(broker, "2024-03-12 14:00:02", 1999.9, 2000.1)
    assert [f.role for f in fills] == ["entry"]
    assert [f.role for f in tick(broker, "2024-03-12 14:00:03", 1999.9, 2000.1)] == ["stop_loss"]


def test_sl_and_tp_in_one_bar_resolve_to_the_stop_without_ticks() -> None:
    broker, recorder = make_broker()
    run_bar(broker, minute_bar("2024-03-12 13:59", (1999.9, 2000.0, 1999.8, 1999.9)))
    send(broker, order(Side.BUY, 0.5, stop=1995.0, target=2010.0), "2024-03-12 13:59:59")
    [entry] = run_bar(broker, minute_bar("2024-03-12 14:00", (1999.9, 2001.0, 1999.0, 2000.0)))
    assert entry.price == pytest.approx(2000.1 * (1 + SLIP))  # the bar's ask open
    assert entry.ts == ns("2024-03-12 14:00")
    # the next bar touches both the stop (low 1994) and the target (high 2011)
    [exit_] = run_bar(broker, minute_bar("2024-03-12 14:01", (2000.0, 2011.0, 1994.0, 2000.0)))
    assert exit_.role == "stop_loss"  # pessimistic: the stop came first
    assert exit_.price == pytest.approx(1995.0 * (1 - SLIP))
    assert exit_.bar_start == ns("2024-03-12 14:01")
    assert exit_.ts == ns("2024-03-12 14:02") - 1
    assert ("cancelled", "O-I000001-TP", "OCO: O-I000001-SL filled") in recorder.rows
    assert broker.ambiguous_bars == 1
    assert broker.bracket_bars == 2  # the entry bar's range and the exit bar
    assert broker.brackets[0].ambiguous


def test_sl_and_tp_in_one_bar_are_resolved_by_ticks_when_they_exist() -> None:
    broker, _ = make_broker()
    long_with_bracket(broker)
    # within one minute the target trades first, then the stop's level
    assert tick(broker, "2024-03-12 14:01:10", 2004.0, 2004.2) == []
    [exit_] = tick(broker, "2024-03-12 14:01:20", 2010.5, 2010.7)
    assert tick(broker, "2024-03-12 14:01:30", 1994.0, 1994.2) == []
    assert exit_.role == "take_profit"
    assert broker.ambiguous_bars == 0


def test_a_bar_opening_beyond_the_stop_fills_at_the_open() -> None:
    broker, _ = make_broker()
    run_bar(broker, minute_bar("2024-03-12 13:59", (1999.9, 2000.0, 1999.8, 1999.9)))
    send(broker, order(Side.BUY, 0.5, stop=1995.0), "2024-03-12 13:59:59")
    run_bar(broker, minute_bar("2024-03-12 14:00", (1999.9, 2000.0, 1999.0, 1999.5)))
    [gap] = run_bar(broker, minute_bar("2024-03-12 14:01", (1990.0, 1991.0, 1989.0, 1990.5)))
    assert gap.price == pytest.approx(1990.0 * (1 - SLIP))
    assert gap.ts == ns("2024-03-12 14:01")
    assert broker.ambiguous_bars == 0


def test_bar_mode_market_orders_fill_at_the_first_open_after_arrival_or_expire() -> None:
    broker, recorder = make_broker()
    run_bar(broker, minute_bar("2024-03-12 13:59", (1999.9, 2000.0, 1999.8, 1999.9)))
    send(broker, order(Side.BUY, 0.5), "2024-03-12 14:00")  # arrives 14:00:01, inside a bar
    assert run_bar(broker, minute_bar("2024-03-12 14:00", (1999.9, 2000.0, 1999.8, 1999.9))) == []
    [fill] = run_bar(broker, minute_bar("2024-03-12 14:01", (2000.9, 2001.0, 2000.8, 2000.9)))
    assert fill.price == pytest.approx(2001.1 * (1 + SLIP))
    send(broker, order(Side.SELL, 0.5, n=2, expected=0.5), "2024-03-12 14:02")
    # the next bar starts ten minutes later: too late for a 300 s fill delay
    assert run_bar(broker, minute_bar("2024-03-12 14:12", (2000.0, 2000.1, 1999.9, 2000.0))) == []
    assert recorder.rows[-1] == ("expired", "O-I000002", "no quote within 300 s of arrival")


# --- closed market ------------------------------------------------------------------------------


def test_no_fill_while_the_market_is_closed_then_a_gap_fill_at_the_reopen() -> None:
    broker, _ = make_broker()
    long_with_bracket(broker)
    # Tuesday 17:00 New York (21:00 UTC, EDT) closes the market until 18:00 (22:00 UTC)
    assert tick(broker, "2024-03-12 21:00:00", 1980.0, 1980.2) == []  # the close itself
    assert tick(broker, "2024-03-12 21:30:00", 1980.0, 1980.2) == []  # a stray quote in the break
    assert broker.position_lots == 0.5
    [stop] = tick(broker, "2024-03-12 22:00:05", 1985.0, 1985.2)
    assert stop.price == pytest.approx(1985.0 * (1 - SLIP))
    assert stop.ts == ns("2024-03-12 22:00:05")


def test_a_market_order_meeting_the_close_expires_rather_than_fill_while_closed() -> None:
    broker, recorder = make_broker()
    tick(broker, "2024-03-12 20:59:58", 1999.9, 2000.1)
    send(broker, order(Side.BUY, 0.5), "2024-03-12 20:59:58")  # arrives 20:59:59
    assert tick(broker, "2024-03-12 21:01:00", 1999.9, 2000.1) == []  # within 300 s, but closed
    assert tick(broker, "2024-03-12 22:00:05", 1999.9, 2000.1) == []  # open, but too late
    assert recorder.rows == [("expired", "O-I000001", "no quote within 300 s of arrival")]


# --- limit and stop entries ---------------------------------------------------------------------


def test_limit_and_stop_entries_trigger_on_the_correct_side() -> None:
    broker, _ = make_broker()
    tick(broker, "2024-03-12 14:00:00", 1999.9, 2000.1)
    send(broker, order(Side.BUY, 0.5, kind="limit", price=1995.0), "2024-03-12 14:00")
    assert tick(broker, "2024-03-12 14:01", 1994.9, 1995.1) == []  # the bid is below, not the ask
    [limit] = tick(broker, "2024-03-12 14:02", 1994.3, 1994.5)
    assert limit.price == 1995.0  # no price improvement
    assert (limit.bid, limit.ask) == (pytest.approx(1994.8), 1995.0)  # its quote at the level
    assert limit.slippage_cost == 0.0
    assert limit.spread_cost == pytest.approx(0.5 * 0.1 * 100)
    send(
        broker,
        order(Side.SELL, 1.0, n=2, kind="stop", price=1990.0, expected=0.5),
        "2024-03-12 14:03",
    )
    [stop] = tick(broker, "2024-03-12 14:04", 1988.0, 1988.2)  # gaps below the sell stop
    assert stop.price == pytest.approx(1988.0 * (1 - SLIP))
    assert stop.role == "entry"  # it flips the long into a short
    assert broker.position_lots == -0.5


# --- rejections and replacement -----------------------------------------------------------------


def test_an_entry_beyond_the_margin_is_rejected_but_an_exit_is_not() -> None:
    broker, recorder = make_broker(equity=4_000.0)
    tick(broker, "2024-03-12 14:00:00", 1999.9, 2000.1)
    # 0.5 lots x 100 oz x 2000 x 5 % = 5,000 USD of margin > 4,000 USD of equity
    assert send(broker, order(Side.BUY, 0.5), "2024-03-12 14:00")[1] is None
    assert recorder.rows[0][0] == "rejected"
    assert "insufficient margin: 5000.00 USD required, equity 4000.00 USD" in recorder.rows[0][2]
    send(broker, order(Side.BUY, 0.3, n=2), "2024-03-12 14:01")  # 3,000 USD: accepted
    tick(broker, "2024-03-12 14:01:02", 1999.9, 2000.1)
    assert broker.position_lots == 0.3


def test_an_order_whose_position_changed_is_rejected_and_leaves_working_orders() -> None:
    broker, recorder = make_broker()
    long_with_bracket(broker)
    # decided while flat, but the account holds 0.5 lots when it arrives
    send(broker, order(Side.BUY, 0.5, n=2, expected=0.0), "2024-03-12 14:05")
    assert recorder.rows[-1][0] == "rejected"
    assert "position changed since the decision" in recorder.rows[-1][2]
    assert broker.orders["O-I000001-SL"].status == "working"


def test_a_new_order_replaces_working_orders_of_earlier_intents() -> None:
    broker, recorder = make_broker()
    long_with_bracket(broker)
    send(broker, order(Side.SELL, 0.5, n=2, expected=0.5), "2024-03-12 14:05")  # exit
    assert ("cancelled", "O-I000001-SL", "replaced by order O-I000002") in recorder.rows
    assert ("cancelled", "O-I000001-TP", "replaced by order O-I000002") in recorder.rows
    [exit_] = tick(broker, "2024-03-12 14:05:02", 1999.9, 2000.1)
    assert (exit_.role, exit_.position_after) == ("exit", 0.0)
    assert broker.brackets[0].closed_at == ns("2024-03-12 14:05:01")


def test_pending_entries_are_cancelled_and_the_end_of_data_cancels_the_rest() -> None:
    broker, recorder = make_broker()
    tick(broker, "2024-03-12 14:00:00", 1999.9, 2000.1)
    send(broker, order(Side.BUY, 0.5, kind="limit", price=1990.0), "2024-03-12 14:00")
    broker.cancel_pending_entries(ns("2024-03-12 14:15"), "superseded by intent I000002")
    assert recorder.rows[-1] == ("cancelled", "O-I000001", "superseded by intent I000002")
    long = order(Side.BUY, 0.5, n=3, stop=1995.0)
    send(broker, long, "2024-03-12 14:16")
    tick(broker, "2024-03-12 14:16:02", 1999.9, 2000.1)
    broker.finish(ns("2024-03-12 15:00"))
    assert recorder.rows[-1] == ("cancelled", "O-I000003-SL", "end of data")
    with pytest.raises(ValueError, match="already submitted"):
        broker.submit(long, ns("2024-03-12 15:00"))


# --- limit orders need a trade through (ADR 0050) ------------------------------------------------


def test_a_target_touched_is_not_filled_one_tick_through_is() -> None:
    broker, _ = make_broker()
    long_with_bracket(broker)  # target 2010: a sell limit, filled against the bid
    assert tick(broker, "2024-03-12 14:01", 2010.00, 2010.20) == []  # touch
    assert tick(broker, "2024-03-12 14:02", 2010.009, 2010.21) == []  # less than a tick through
    [fill] = tick(broker, "2024-03-12 14:03", 2010.01, 2010.21)  # one tick through
    assert fill.role == "take_profit"
    assert fill.price == 2010.0  # at the limit, never better


def test_a_buy_limit_needs_the_ask_a_tick_below_it() -> None:
    broker, _ = make_broker()
    tick(broker, "2024-03-12 14:00:00", 1999.9, 2000.1)
    send(broker, order(Side.BUY, 0.5, kind="limit", price=1995.0), "2024-03-12 14:00")
    assert tick(broker, "2024-03-12 14:01", 1994.8, 1995.00) == []  # the ask touches 1995
    [fill] = tick(broker, "2024-03-12 14:02", 1994.79, 1994.99)
    assert fill.price == 1995.0


def test_in_bar_mode_a_touched_target_neither_fills_nor_makes_the_bar_ambiguous() -> None:
    broker, _ = make_broker()
    run_bar(broker, minute_bar("2024-03-12 13:59", (1999.9, 2000.0, 1999.8, 1999.9)))
    send(broker, order(Side.BUY, 0.5, stop=1995.0, target=2010.0), "2024-03-12 13:59:59")
    run_bar(broker, minute_bar("2024-03-12 14:00", (1999.9, 2001.0, 1999.0, 2000.0)))
    # the range touches the target (high 2010.00) and reaches the stop (low 1994): not ambiguous
    [stop] = run_bar(broker, minute_bar("2024-03-12 14:01", (2000.0, 2010.0, 1994.0, 2000.0)))
    assert stop.role == "stop_loss"
    assert broker.ambiguous_bars == 0
    # one tick through the target, the stop reached too: ambiguous, resolved to the stop
    broker, _ = make_broker()
    run_bar(broker, minute_bar("2024-03-12 13:59", (1999.9, 2000.0, 1999.8, 1999.9)))
    send(broker, order(Side.BUY, 0.5, stop=1995.0, target=2010.0), "2024-03-12 13:59:59")
    run_bar(broker, minute_bar("2024-03-12 14:00", (1999.9, 2001.0, 1999.0, 2000.0)))
    [stop] = run_bar(broker, minute_bar("2024-03-12 14:01", (2000.0, 2010.01, 1994.0, 2000.0)))
    assert stop.role == "stop_loss"
    assert broker.ambiguous_bars == 1


def test_in_bar_mode_a_limit_fills_at_its_price_only_through_the_range_or_the_open() -> None:
    broker, _ = make_broker()
    run_bar(broker, minute_bar("2024-03-12 13:59", (1999.9, 2000.0, 1999.8, 1999.9)))
    send(broker, order(Side.BUY, 0.5, target=2010.0), "2024-03-12 13:59:59")
    run_bar(broker, minute_bar("2024-03-12 14:00", (1999.9, 2001.0, 1999.0, 2000.0)))
    assert run_bar(broker, minute_bar("2024-03-12 14:01", (2005.0, 2010.0, 2004.0, 2009.0))) == []
    [gap] = run_bar(broker, minute_bar("2024-03-12 14:02", (2012.0, 2013.0, 2011.0, 2012.5)))
    assert (gap.price, gap.ts) == (2010.0, ns("2024-03-12 14:02"))  # the open went through
