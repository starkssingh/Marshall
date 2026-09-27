"""RISK-002 properties: a size never exceeds the requested exposure or the risk budget to the
stop, is a lot-step multiple within the lot range, and shrinks with the drawdown."""

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.event_backtest import CFG, INSTRUMENT
from xq.risk.sizing import drawdown_throttle, probability_scale, size_entry

SIZING = CFG.risk_config().sizing
CONTRACT = float(INSTRUMENT.contract_size)
REL = 1e-9

inputs = st.fixed_dictionaries(
    {
        "equity": st.floats(1_000, 50_000_000),
        "price": st.floats(500, 5_000),
        "stop_distance": st.floats(0.01, 200),
        "sigma_daily": st.floats(0.001, 0.1),
        "requested_exposure": st.floats(0, 20),
        "p_win": st.none() | st.floats(0, 1),
        "drawdown": st.floats(0, 0.5),
    }
)


@settings(max_examples=500, deadline=None)
@given(inputs)
def test_a_size_never_exceeds_the_request_or_the_risk_budget(case: dict[str, float]) -> None:
    result = size_entry(SIZING, INSTRUMENT, periods_per_year=252, **case)  # type: ignore[arg-type]
    lots = result.lots
    requested = case["requested_exposure"] * case["equity"] / (case["price"] * CONTRACT)
    budget = SIZING.risk_per_trade * case["equity"]
    assert lots <= requested * (1 + REL) + 1e-12
    assert lots * case["stop_distance"] * CONTRACT <= budget * (1 + REL) + 1e-9
    assert lots <= float(INSTRUMENT.max_lot)
    assert lots == 0 or lots >= float(INSTRUMENT.min_lot)
    steps = Decimal(str(lots)) / INSTRUMENT.lot_step
    assert steps == steps.to_integral_value()
    assert result.raw_lots >= lots - 1e-9 or lots == float(INSTRUMENT.max_lot)


@settings(max_examples=300, deadline=None)
@given(st.floats(0, 1), st.floats(0, 1))
def test_the_scales_are_monotone_and_within_zero_and_one(a: float, b: float) -> None:
    low, high = min(a, b), max(a, b)
    assert 0 <= drawdown_throttle(high, 0.05, 0.15) <= drawdown_throttle(low, 0.05, 0.15) <= 1
    assert 0 <= probability_scale(low, 0.5, 0.6) <= probability_scale(high, 0.5, 0.6) <= 1
