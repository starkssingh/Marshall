"""RISK-001: the risk state — drawdown and its sticky worst, the trading day's P&L and entries,
consecutive losing round trips — and its reconstruction from the decision ledger."""

from datetime import date

import pandas as pd
import pytest

from helpers.event_backtest import (
    CAPITAL,
    CLOCK,
    INSTRUMENT,
    RandomStrategy,
    exact_costs,
    ns,
    random_quotes,
)
from xq.backtest.broker_sim import SimulatedBroker
from xq.backtest.engine import EventEngine, MarketData
from xq.backtest.events import AccountState, Fill
from xq.backtest.ledger import Ledger
from xq.backtest.portfolio import Portfolio
from xq.core.types import Timeframe
from xq.risk.placeholder import PassThroughRiskApprover
from xq.risk.state import MarketState, RiskStateTracker, rebuild_risk_state


def account(at: str, equity: float, position: float = 0.0) -> AccountState:
    return AccountState(ns(at), CAPITAL, equity, 0.0, equity, position, 0.0, 2000.0)


def fill(at: str, lots: float, price: float, role: str = "entry", after: float = 0.0) -> Fill:
    return Fill(
        fill_id="F",
        order_id="O",
        decision_id="D",
        intent_id="I",
        ts=ns(at),
        decided_at=ns(at),
        role=role,
        order_type="market",
        lots=lots,
        price=price,
        bid=price,
        ask=price,
        mid=price,
        slippage_bps=0.0,
        spread_cost=0.0,
        slippage_cost=0.0,
        commission=0.0,
        position_after=after,
    )


def test_drawdown_is_from_the_peak_and_its_worst_is_sticky() -> None:
    tracker = RiskStateTracker(CAPITAL, 100.0)
    tracker.observe(account("2024-03-12 14:00", 100_000.0))
    tracker.observe(account("2024-03-12 15:00", 110_000.0))
    tracker.observe(account("2024-03-12 16:00", 99_000.0))
    state = tracker.state()
    assert state.peak_equity == 110_000.0
    assert state.drawdown == pytest.approx(0.1)
    tracker.observe(account("2024-03-12 16:30", 105_000.0))
    state = tracker.state()
    assert state.drawdown == pytest.approx(5_000 / 110_000)
    assert state.worst_drawdown == pytest.approx(0.1)  # sticky until a manual reset
    tracker.reset_halt(account("2024-03-12 16:45", 105_000.0))
    assert tracker.state().worst_drawdown == 0.0
    assert tracker.state().peak_equity == 105_000.0


def test_the_trading_day_starts_at_the_previous_close_and_counts_its_entries() -> None:
    tracker = RiskStateTracker(CAPITAL, 100.0)
    tracker.observe(account("2024-03-12 14:00", 100_000.0))
    tracker.on_fill(fill("2024-03-12 14:00:02", 0.5, 2000.0, after=0.5))
    tracker.on_fill(fill("2024-03-12 15:00:02", 0.5, 2000.0, after=1.0))
    tracker.on_fill(fill("2024-03-12 16:00:02", -1.0, 2000.0, role="exit", after=0.0))
    tracker.observe(account("2024-03-12 16:10", 100_500.0))
    state = tracker.state()
    assert state.trading_day == date(2024, 3, 12)
    assert (state.trades_today, state.day_start_equity, state.day_pnl) == (2, CAPITAL, 500.0)
    tracker.close_day(account("2024-03-12 21:00", 100_400.0))  # Tuesday's end (17:00 EDT)
    tracker.observe(account("2024-03-12 22:30", 100_100.0))  # Wednesday's trading day
    state = tracker.state()
    assert state.trading_day == date(2024, 3, 13)
    assert (state.trades_today, state.day_start_equity) == (0, 100_400.0)
    assert state.day_pnl == pytest.approx(-300.0)
    assert state.day_loss == pytest.approx(300.0 / 100_400.0)


def test_consecutive_losses_count_losing_round_trips_including_a_flip() -> None:
    tracker = RiskStateTracker(CAPITAL, 100.0)
    tracker.observe(account("2024-03-12 14:00", 100_000.0))
    tracker.on_fill(fill("2024-03-12 14:00:02", 0.5, 2000.0, after=0.5))
    tracker.on_fill(fill("2024-03-12 14:30:02", -0.5, 1990.0, role="exit"))  # loss
    tracker.on_fill(fill("2024-03-12 15:00:02", -0.5, 1990.0, after=-0.5))
    # a flip: the short closes at a loss (1995 > 1990) and a long opens
    tracker.on_fill(fill("2024-03-12 15:30:02", 1.0, 1995.0, after=0.5))
    state_after_flip = tracker.state()
    assert state_after_flip.consecutive_losses == 2
    assert state_after_flip.last_loss_at == ns("2024-03-12 15:30:02")
    tracker.on_fill(fill("2024-03-12 16:00:02", -0.5, 2005.0, role="exit"))  # a win
    assert tracker.state().consecutive_losses == 0
    assert tracker.state().position_lots == 0.0


def test_the_market_state_knows_its_quote_age_and_spread() -> None:
    market = MarketState(ns("2024-03-12 14:00:30"), 1999.9, 2000.1, ns("2024-03-12 14:00:00"))
    assert market.quote_age_s == 30.0
    assert market.spread == pytest.approx(0.2)
    assert market.mid == pytest.approx(2000.0)


def test_a_state_needs_an_observation() -> None:
    with pytest.raises(ValueError, match="observation"):
        RiskStateTracker(CAPITAL, 100.0).state()


@pytest.mark.parametrize("seed", [2, 3])
def test_the_rebuilt_state_equals_the_live_state_at_every_decision(seed: int) -> None:
    costs = exact_costs()
    q = random_quotes("2024-03-11 00:00", "2024-03-16 00:00", seed=seed, every_s=30)
    portfolio = Portfolio(costs, capital=CAPITAL, margin_rate=0.05)
    ledger = Ledger()
    broker = SimulatedBroker(
        costs, CLOCK, recorder=ledger, margin_rate=0.05, equity=lambda ts: portfolio.equity
    )
    engine = EventEngine(
        RandomStrategy(seed=seed, trade_probability=0.2),
        MarketData.from_ticks(q, Timeframe.M15),
        broker=broker,
        risk=PassThroughRiskApprover(INSTRUMENT, CAPITAL),
        account=portfolio,
        recorder=ledger,
        costs=costs,
        clock=CLOCK,
    )
    live = engine.run().risk_states
    frame = ledger.frame()
    decisions = frame.loc[(frame["kind"] == "account") & (frame["reason"] == "decision"), "seq"]
    assert len(live) == len(decisions) > 50
    assert max(s.consecutive_losses for s in live) >= 2
    assert max(s.trades_today for s in live) >= 2
    for state, seq in zip(live, decisions, strict=True):
        rebuilt = rebuild_risk_state(frame, capital=CAPITAL, contract_size=100.0, upto_seq=int(seq))
        assert rebuilt == state
    days = frame.loc[(frame["kind"] == "account") & (frame["reason"] == "day_end")]
    assert pd.DatetimeIndex(days["ts"]).is_monotonic_increasing
    assert len(days) >= 5
