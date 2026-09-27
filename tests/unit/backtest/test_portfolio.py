"""BT-006: portfolio accounting — FIFO trades on a hand-computed case, financing over the triple
rollover on long and short positions, and equity = cash + unrealized (and the independent
mark-to-market equity) after every event of an engine run."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from helpers.event_backtest import (
    CAPITAL,
    CLOCK,
    INSTRUMENT,
    CallRecorder,
    RandomStrategy,
    ScriptedStrategy,
    exact_costs,
    ns,
    quotes,
    random_quotes,
)
from xq.backtest.broker_sim import SimulatedBroker
from xq.backtest.costs import CostModel
from xq.backtest.engine import EventEngine, MarketData, Strategy
from xq.backtest.events import Event, Fill, Quote
from xq.backtest.portfolio import Portfolio, daily_frame, fills_frame
from xq.core.types import Timeframe
from xq.risk.placeholder import PassThroughRiskApprover
from xq.signals.schema import TradeIntent


def fill(n: int, lots: float, price: float, at: str = "2024-03-12 14:00") -> Fill:
    return Fill(
        fill_id=f"F{n}",
        order_id=f"O{n}",
        decision_id=f"D{n}",
        intent_id=f"I{n}",
        ts=ns(at),
        decided_at=ns(at),
        role="entry",
        order_type="market",
        lots=lots,
        price=price,
        bid=price,
        ask=price,
        mid=price,
        slippage_bps=0.0,
        spread_cost=0.0,
        slippage_cost=0.0,
        commission=abs(lots) * 3.5,
        position_after=0.0,
    )


def wire(
    strategy: Strategy, data: MarketData, costs: CostModel, *, observe: bool = False
) -> tuple[EventEngine, Portfolio, list[tuple[float, float, float]]]:
    portfolio = Portfolio(costs, capital=CAPITAL, margin_rate=0.05)
    recorder = CallRecorder()
    broker = SimulatedBroker(
        costs, CLOCK, recorder=recorder, margin_rate=0.05, equity=lambda ts: portfolio.equity
    )
    checks: list[tuple[float, float, float]] = []

    def observer(event: Event, now: int) -> None:
        state = portfolio.state(now)
        checks.append(
            (state.equity, state.cash + state.unrealized, portfolio.equity_mark_to_market())
        )

    engine = EventEngine(
        strategy,
        data,
        broker=broker,
        risk=PassThroughRiskApprover(INSTRUMENT, CAPITAL),
        account=portfolio,
        recorder=recorder,
        costs=costs,
        clock=CLOCK,
        observer=observer if observe else None,
    )
    return engine, portfolio, checks


def test_fifo_trades_match_a_hand_computation() -> None:
    portfolio = Portfolio(exact_costs(), capital=CAPITAL, margin_rate=0.05)
    portfolio.mark(Quote(0, 1999.9, 2000.1))
    portfolio.book(fill(1, 0.3, 2000.0))
    portfolio.book(fill(2, 0.2, 2010.0))
    portfolio.book(fill(3, -0.4, 2020.0, "2024-03-12 15:00"))  # closes 0.3 of lot 1, 0.1 of lot 2
    portfolio.mark(Quote(0, 2029.9, 2030.1))
    trades = portfolio.trades_frame()
    closed = trades.loc[~trades["open"]]
    np.testing.assert_allclose(closed["lots"], [0.3, 0.1])
    np.testing.assert_allclose(closed["price_pnl"], [0.3 * 20 * 100, 0.1 * 10 * 100])
    # commission: the opening fill's share plus the closing fill's per-lot charge
    np.testing.assert_allclose(closed["commission"], [1.05 + 0.3 * 3.5, 0.7 * 0.5 + 0.1 * 3.5])
    assert closed["entry_fill_id"].tolist() == ["F1", "F2"]
    open_ = trades.loc[trades["open"]].iloc[0]
    assert (open_["lots"], open_["entry_price"]) == (pytest.approx(0.1), 2010.0)
    assert open_["price_pnl"] == pytest.approx(0.1 * 20 * 100)  # marked at 2030
    assert open_["commission"] == pytest.approx(0.35)
    assert portfolio.position_lots == pytest.approx(0.1)
    assert portfolio.cash == pytest.approx(CAPITAL + 700 - 3.15)
    assert portfolio.unrealized == pytest.approx(200.0)
    assert portfolio.equity == pytest.approx(100_896.85)
    assert portfolio.equity_mark_to_market() == pytest.approx(portfolio.equity)
    assert trades["pnl"].sum() == pytest.approx(portfolio.equity - CAPITAL)
    state = portfolio.state(0)
    assert state.margin_used == pytest.approx(0.1 * 100 * 2030 * 0.05)


def test_a_flip_closes_the_lots_and_opens_the_rest_on_the_other_side() -> None:
    portfolio = Portfolio(exact_costs(), capital=CAPITAL, margin_rate=0.05)
    portfolio.mark(Quote(0, 1999.9, 2000.1))
    portfolio.book(fill(1, 0.5, 2000.0))
    portfolio.book(fill(2, -0.8, 1990.0))
    assert portfolio.position_lots == pytest.approx(-0.3)
    [lot] = portfolio.lots
    assert (lot.lots, lot.price, lot.fill_id) == (pytest.approx(-0.3), 1990.0, "F2")
    assert lot.commission == pytest.approx(0.3 * 3.5)
    assert portfolio.realized == pytest.approx(-0.5 * 10 * 100)


def financing_run(direction: str) -> tuple[Portfolio, pd.DataFrame]:
    costs = exact_costs(long_rate=6.0, short_rate=2.0)
    q = quotes(
        ("2024-03-12 14:00:00", 1999.9, 2000.1),  # Tuesday
        ("2024-03-12 14:15:00", 1999.9, 2000.1),  # the decision's quote
        ("2024-03-12 14:15:02", 1999.9, 2000.1),  # the entry
        ("2024-03-12 20:59:00", 2009.9, 2010.1),  # the mark over Tuesday's rollover (x1)
        ("2024-03-13 20:59:00", 2019.9, 2020.1),  # over Wednesday's rollover (x3, the weekend)
        ("2024-03-14 14:00:00", 2019.9, 2020.1),  # Thursday
        ("2024-03-14 14:15:02", 2019.9, 2020.1),  # the exit
    )
    intent = TradeIntent(direction=direction, exposure=1.0)  # type: ignore[arg-type]
    strategy = ScriptedStrategy(
        {"2024-03-12 14:15": [intent], "2024-03-14 14:15": [TradeIntent(direction="flat")]}
    )
    engine, portfolio, _ = wire(strategy, MarketData.from_ticks(q, Timeframe.M15), costs)
    output = engine.run()
    daily = daily_frame(
        [(s.day, s.state) for s in output.day_states],
        fills_frame(output.fills),
        portfolio.financing_series(),
        capital=CAPITAL,
        contract=100.0,
    )
    return portfolio, daily


@pytest.mark.parametrize(("direction", "rate"), [("long", 0.06), ("short", 0.02)])
def test_financing_over_the_triple_rollover_is_charged_on_both_sides(
    direction: str, rate: float
) -> None:
    portfolio, daily = financing_run(direction)
    charges = portfolio.charges
    assert [c.ts for c in charges] == [ns("2024-03-12 21:00"), ns("2024-03-13 21:00")]
    assert [c.multiplier for c in charges] == [1, 3]
    # 0.5 lots x 100 oz at the mid of the last quote before each rollover, act/360
    expected = [0.5 * 100 * 2010.0 * rate / 360, 3 * 0.5 * 100 * 2020.0 * rate / 360]
    np.testing.assert_allclose([c.amount for c in charges], expected)
    assert all(c.amount > 0 for c in charges)  # a cost on longs and on shorts (ADR 0032)
    assert daily.index.tolist() == [date(2024, 3, 12), date(2024, 3, 13), date(2024, 3, 14)]
    np.testing.assert_allclose(daily["financing"], [*expected, 0.0])
    trade = portfolio.trades_frame().iloc[0]
    assert trade["financing"] == pytest.approx(sum(expected))
    assert trade["pnl"] == pytest.approx(portfolio.equity - CAPITAL)
    np.testing.assert_allclose(
        daily["position_lots"], [0.5 if direction == "long" else -0.5] * 2 + [0.0]
    )


def test_equity_is_cash_plus_unrealized_after_every_event() -> None:
    q = random_quotes("2024-03-11 00:00", "2024-03-16 00:00", seed=5, every_s=30)
    data = MarketData.from_ticks(q, Timeframe.M15)
    engine, portfolio, checks = wire(
        RandomStrategy(seed=3, trade_probability=0.2), data, exact_costs(), observe=True
    )
    output = engine.run()
    assert len(checks) == output.events > 10_000
    values = np.array(checks)
    np.testing.assert_allclose(values[:, 0], values[:, 1], rtol=0, atol=1e-9)
    np.testing.assert_allclose(values[:, 0], values[:, 2], rtol=0, atol=1e-6)
    roles = {f.role for f in output.fills}
    assert {"entry", "exit", "stop_loss", "take_profit"} <= roles
    assert len(portfolio.charges) >= 2  # rollovers held over
    for charge in portfolio.charges:  # 3.6 %/yr on either side, act/360
        expected = abs(charge.lots) * 100 * charge.mark * 0.036 / 360 * charge.multiplier
        assert charge.amount == pytest.approx(expected)
    trades = portfolio.trades_frame()
    assert trades["pnl"].sum() == pytest.approx(portfolio.equity - CAPITAL)
    daily = daily_frame(
        [(s.day, s.state) for s in output.day_states],
        fills_frame(output.fills),
        portfolio.financing_series(),
        capital=CAPITAL,
        contract=100.0,
    )
    costs = daily[["spread_cost", "slippage_cost", "commission", "financing"]].sum(axis=1)
    np.testing.assert_allclose(daily["net_pnl"], daily["gross_pnl"] - costs, atol=1e-9)
    assert daily["equity"].iloc[-1] == pytest.approx(portfolio.equity)
    assert daily["commission"].sum() == pytest.approx(portfolio.commission)
    assert daily["financing"].sum() == pytest.approx(portfolio.financing)
