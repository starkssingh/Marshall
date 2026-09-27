"""RISK-002: sizing on hand-computed cases — fixed fractional, volatility targeting, the requested
exposure cap, edge-per-unit-risk scaling of a calibrated probability (ADR 0053), the drawdown
throttle and lot rounding."""

import math

import pytest

from helpers.event_backtest import CFG, INSTRUMENT
from xq.core.config import SizingConfig
from xq.risk.sizing import (
    Edge,
    drawdown_throttle,
    edge_per_risk,
    edge_scale,
    fixed_fractional_lots,
    size_entry,
    vol_target_lots,
)

SIZING = CFG.risk_config().sizing


def sized(**changes: object) -> float:
    args: dict[str, object] = {
        "equity": 100_000.0,
        "price": 2000.0,
        "stop_distance": 5.0,
        "sigma_daily": 0.01,
        "periods_per_year": 252,
        "requested_exposure": 10.0,
        "edge": None,
        "drawdown": 0.0,
    }
    config = changes.pop("config", SIZING)
    args.update(changes)
    return size_entry(config, INSTRUMENT, **args).lots  # type: ignore[arg-type]


def test_the_default_profile_is_the_owners_plan_default() -> None:
    assert (SIZING.method, SIZING.risk_per_trade) == ("fixed_fractional", 0.005)
    assert CFG.risk_config().limits.max_drawdown == 0.15


def test_fixed_fractional_risks_the_budget_to_the_stop() -> None:
    # 0.5 % of 100,000 = 500 USD over a 5 USD stop on 100 oz per lot: 1.0 lot
    assert fixed_fractional_lots(100_000.0, 0.005, 5.0, 100.0) == pytest.approx(1.0)
    assert sized() == 1.0
    assert sized(stop_distance=7.0) == 0.71  # 0.714... rounded down
    with pytest.raises(ValueError, match="stop distance"):
        fixed_fractional_lots(100_000.0, 0.005, 0.0, 100.0)


def test_vol_targeting_sizes_the_annualized_volatility() -> None:
    # 10 % / (1 % x sqrt(252)) = 0.630 of equity; 63,000 USD / (2000 x 100) = 0.315 lots
    exposure = 0.10 / (0.01 * math.sqrt(252))
    assert vol_target_lots(100_000.0, 0.10, 0.01, 252, 2000.0, 100.0) == pytest.approx(
        exposure * 100_000 / 200_000
    )
    vol = SizingConfig.model_validate({**SIZING.model_dump(), "method": "vol_target"})
    assert sized(config=vol) == 0.31
    with pytest.raises(ValueError, match="sigma"):
        sized(config=vol, sigma_daily=None)


def test_the_requested_exposure_caps_the_size() -> None:
    assert sized(requested_exposure=0.5) == 0.25  # 50,000 USD / (2000 x 100 oz)
    assert sized(requested_exposure=0.0) == 0.0


def edge(p: float, *, p_se: float = 0.0, payoff: float = 1.0, cost: float = 0.0) -> Edge:
    """An edge on the 5 USD stop of `sized`: the target `payoff` stops away."""
    return Edge(p, p_se, payoff * 5.0, cost)


def test_the_edge_profile_is_provisional_and_conservative() -> None:
    assert (SIZING.ev_r_full, SIZING.lcb_z) == (0.25, 1.645)


def test_payoffs_at_their_break_even_get_no_size() -> None:
    # 1:1 at p = 1/2 and 2:1 at p = 1/3 expect nothing per unit of risk
    assert edge_per_risk(0.5, 1.0, 0.0) == 0.0
    assert edge_per_risk(1 / 3, 2.0, 0.0) == pytest.approx(0.0, abs=1e-15)
    assert sized(edge=edge(0.5)) == 0.0
    assert sized(edge=edge(1 / 3, payoff=2.0)) == 0.0
    # below break-even the multiplier is zero, not negative
    assert edge_scale(edge_per_risk(0.3, 2.0, 0.0), 0.25) == 0.0


def test_a_two_to_one_trade_at_p_0_45_gets_a_positive_size() -> None:
    # the raw-p scaling it replaces (zero below p = 0.5) gave this trade nothing
    # p_lcb = 0.45 - 1.645 x 0.05 = 0.36775; ev_r = 0.36775 x 2 - 0.63225 = 0.10325
    result = size_entry(
        SIZING,
        INSTRUMENT,
        equity=100_000.0,
        price=2000.0,
        stop_distance=5.0,
        sigma_daily=0.01,
        periods_per_year=252,
        requested_exposure=10.0,
        edge=edge(0.45, p_se=0.05, payoff=2.0),
        drawdown=0.0,
    )
    assert result.p_lcb == pytest.approx(0.36775)
    assert result.ev_r == pytest.approx(0.10325)
    assert result.edge_scale == pytest.approx(0.10325 / 0.25)
    assert result.lots == 0.41  # 1.0 lot x 0.413, rounded down
    assert result.as_snapshot()["ev_r"] == pytest.approx(0.10325)


def test_costs_and_uncertainty_shrink_the_size() -> None:
    free = sized(edge=edge(0.45, p_se=0.05, payoff=2.0))
    # a round trip of 0.5 USD against the 5 USD stop costs 0.1 of the risk: ev_r 0.00325
    costly = sized(edge=edge(0.45, p_se=0.05, payoff=2.0, cost=0.5))
    assert costly == 0.01
    assert 0 < costly < free
    assert sized(edge=edge(0.45, p_se=0.10, payoff=2.0)) == 0.0  # p_lcb 0.2855: below 1/3
    # a strong edge reaches full size, and the size is never more than without scaling
    assert sized(edge=edge(0.7, p_se=0.02)) == sized() == 1.0


def test_the_edge_inputs_are_validated() -> None:
    with pytest.raises(ValueError, match="probability"):
        Edge(1.2, 0.0, 5.0, 0.0)
    with pytest.raises(ValueError, match="target distance"):
        Edge(0.5, 0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="ev_r_full"):
        edge_scale(0.1, 0.0)


def test_drawdown_scales_the_size_down() -> None:
    assert drawdown_throttle(0.05, 0.05, 0.15) == 1.0
    assert drawdown_throttle(0.10, 0.05, 0.15) == pytest.approx(0.5)
    assert drawdown_throttle(0.15, 0.05, 0.15) == 0.0
    assert sized(drawdown=0.10) == 0.5
    assert sized(drawdown=0.15) == 0.0


def test_sizes_round_down_to_the_lot_step_within_the_lot_range() -> None:
    assert sized(stop_distance=5.0 / 0.999) == 0.99  # 0.999 lots rounds down
    assert sized(requested_exposure=0.0019) == 0.0  # 0.00095 lots: below the minimum lot
    assert sized(stop_distance=0.001, requested_exposure=1e4) == float(INSTRUMENT.max_lot)
    assert sized(equity=-10.0) == 0.0
