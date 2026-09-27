"""RISK-002: sizing on hand-computed cases — fixed fractional, volatility targeting, the requested
exposure cap, calibrated-probability scaling, the drawdown throttle and lot rounding."""

import math

import pytest

from helpers.event_backtest import CFG, INSTRUMENT
from xq.core.config import SizingConfig
from xq.risk.sizing import (
    drawdown_throttle,
    fixed_fractional_lots,
    probability_scale,
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
        "p_win": None,
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


def test_probability_and_drawdown_scale_the_size_down() -> None:
    assert probability_scale(None, 0.5, 0.6) == 1.0
    assert probability_scale(0.5, 0.5, 0.6) == 0.0
    assert probability_scale(0.55, 0.5, 0.6) == pytest.approx(0.5)
    assert probability_scale(0.9, 0.5, 0.6) == 1.0  # capped
    assert sized(p_win=0.55) == 0.5
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
