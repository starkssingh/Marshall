"""RISK-003 properties: a capped target never exceeds any limit — lots, notional (with correlated
exposure), margin, session exposure — nor the size asked for, keeps its side, and is a lot-step
multiple."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.event_backtest import CFG, INSTRUMENT
from xq.risk.limits import cap_target
from xq.risk.state import MarketState, RiskState

LIMITS = CFG.risk_config().limits
CONTRACT = float(INSTRUMENT.contract_size)
REL = 1e-9
STATE = RiskState(
    ts=0,
    trading_day=date(2024, 3, 12),
    capital=100_000.0,
    equity=100_000.0,
    peak_equity=100_000.0,
    drawdown=0.0,
    worst_drawdown=0.0,
    day_start_equity=100_000.0,
    day_pnl=0.0,
    position_lots=0.0,
    mark=2000.0,
    open_notional=0.0,
    margin_used=0.0,
    consecutive_losses=0,
    last_loss_at=None,
    trades_today=0,
)

cases = st.fixed_dictionaries(
    {
        "target": st.floats(-500, 500),
        "equity": st.floats(-10_000, 50_000_000),
        "price": st.floats(500, 5_000),
        "margin_rate": st.floats(0.01, 1.0),
        "correlated": st.floats(0, 5_000_000),
        "max_lots": st.floats(0.01, 100),
        "max_notional": st.floats(0.1, 20),
        "max_margin_use": st.floats(0.01, 1.0),
        "asia": st.none() | st.floats(0.0, 10),
        "in_asia": st.booleans(),
    }
)


@settings(max_examples=1000, deadline=None)
@given(cases)
def test_a_capped_target_never_exceeds_any_limit(case: dict[str, float]) -> None:
    sessions = {} if case["asia"] is None else {"asia": case["asia"]}
    limits = LIMITS.model_copy(
        update={
            "max_lots": case["max_lots"],
            "max_notional": case["max_notional"],
            "max_margin_use": case["max_margin_use"],
            "session_max_exposure": sessions,
        }
    )
    market = MarketState(0, 1999.9, 2000.1, 0, sessions=("asia",) if case["in_asia"] else ())
    state = replace(STATE, equity=case["equity"])
    lots, _ = cap_target(
        case["target"],
        state=state,
        market=market,
        limits=limits,
        instrument=INSTRUMENT,
        margin_rate=case["margin_rate"],
        price=case["price"],
        correlated_notional=case["correlated"],
    )
    size = abs(lots)
    equity = max(case["equity"], 0.0)
    notional = size * CONTRACT * case["price"]
    assert size <= abs(case["target"]) * (1 + REL)
    assert size <= case["max_lots"] * (1 + REL)
    assert size <= float(INSTRUMENT.max_lot)
    if size > 0:  # correlated exposure alone may already exceed the cap: then nothing is added
        assert notional + case["correlated"] <= case["max_notional"] * equity * (1 + REL) + 1e-6
    assert notional * case["margin_rate"] <= case["max_margin_use"] * equity * (1 + REL) + 1e-6
    if case["in_asia"] and case["asia"] is not None:
        assert notional <= case["asia"] * equity * (1 + REL) + 1e-6
    assert lots == 0 or (lots > 0) == (case["target"] > 0)
    assert lots == 0 or size >= float(INSTRUMENT.min_lot)
    steps = Decimal(str(size)) / INSTRUMENT.lot_step
    assert steps == steps.to_integral_value()
