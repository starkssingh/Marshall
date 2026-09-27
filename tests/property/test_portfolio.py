"""BT-006 property: for any sequence of fills, marks and rollover charges, equity = cash +
unrealized equals the mark-to-market equity, the position is the sum of the fills, and the FIFO
trades' P&L adds up to the change in equity."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.event_backtest import CAPITAL, exact_costs
from xq.backtest.events import Fill, Quote, clean_lots
from xq.backtest.portfolio import Portfolio

COSTS = exact_costs(long_rate=6.0, short_rate=2.0)

step = st.one_of(
    st.tuples(
        st.just("fill"),
        st.integers(-150, 150).filter(lambda n: n != 0),  # lots in 0.01 steps
        st.floats(1800, 2200, allow_nan=False),
    ),
    st.tuples(st.just("mark"), st.just(0), st.floats(1800, 2200, allow_nan=False)),
    st.tuples(st.just("roll"), st.integers(1, 3), st.just(0.0)),
)


@settings(max_examples=200, deadline=None)
@given(st.lists(step, min_size=1, max_size=40))
def test_accounting_identities_hold_for_any_sequence(steps: list[tuple[str, int, float]]) -> None:
    portfolio = Portfolio(COSTS, capital=CAPITAL, margin_rate=0.05)
    portfolio.mark(Quote(0, 1999.9, 2000.1))
    position = 0.0
    for n, (kind, amount, price) in enumerate(steps):
        if kind == "mark":
            portfolio.mark(Quote(n, price - 0.1, price + 0.1))
        elif kind == "roll":
            portfolio.charge_financing(n, amount)
        else:
            lots = amount / 100
            position = clean_lots(position + lots)
            portfolio.book(
                Fill(
                    fill_id=f"F{n}",
                    order_id=f"O{n}",
                    decision_id=f"D{n}",
                    intent_id=f"I{n}",
                    ts=n,
                    decided_at=n,
                    role="entry",
                    order_type="market",
                    lots=lots,
                    price=price,
                    bid=price - 0.1,
                    ask=price + 0.1,
                    mid=price,
                    slippage_bps=0.0,
                    spread_cost=abs(lots) * 0.1 * 100,
                    slippage_cost=0.0,
                    commission=abs(lots) * 3.5,
                    position_after=position,
                )
            )
        state = portfolio.state(n)
        assert state.equity == pytest.approx(state.cash + state.unrealized, abs=1e-9)
        assert portfolio.equity_mark_to_market() == pytest.approx(state.equity, abs=1e-6)
        assert portfolio.position_lots == position
    trades = portfolio.trades_frame()
    assert trades["pnl"].sum() == pytest.approx(portfolio.equity - CAPITAL, abs=1e-6)
    assert (trades["lots"] > 0).all()
