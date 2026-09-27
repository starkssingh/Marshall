"""RISK-002 properties: a size never exceeds the requested exposure or the risk budget to the
stop, is a lot-step multiple within the lot range, and shrinks with the drawdown; the edge
multiplier (ADR 0053) lies in [0, 1], is zero at or below break-even, grows with p and shrinks with
costs and uncertainty."""

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.event_backtest import CFG, INSTRUMENT
from xq.risk.sizing import Edge, drawdown_throttle, edge_per_risk, edge_scale, size_entry

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
        "edge": st.none()
        | st.builds(
            Edge,
            st.floats(0, 1),
            st.floats(0, 0.2),
            st.floats(0.01, 400),
            st.floats(0, 20),
        ),
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


@settings(max_examples=500, deadline=None)
@given(
    st.floats(0, 1),
    st.floats(0, 1),
    st.floats(0.1, 5),
    st.floats(0, 0.5),
    st.floats(0, 0.5),
)
def test_the_edge_multiplier_is_bounded_monotone_and_zero_at_break_even(
    p1: float, p2: float, payoff: float, c1: float, c2: float
) -> None:
    low, high = min(p1, p2), max(p1, p2)
    cheap, dear = min(c1, c2), max(c1, c2)
    full = SIZING.ev_r_full
    scales = [
        edge_scale(edge_per_risk(p, payoff, c), full) for p in (low, high) for c in (cheap, dear)
    ]
    assert all(0 <= s <= 1 for s in scales)
    low_cheap, low_dear, high_cheap, high_dear = scales
    assert low_cheap <= high_cheap  # more probability, more size
    assert low_dear <= high_dear
    assert low_dear <= low_cheap  # more cost, less size
    assert high_dear <= high_cheap
    break_even = (1 + dear) / (1 + payoff)  # p at which ev_r = 0 with the dearer cost
    if high <= break_even:
        assert edge_scale(edge_per_risk(high, payoff, dear), full) == 0
