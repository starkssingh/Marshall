"""ROB-004: Monte Carlo equity with the risk rules applied — replayed through the real risk
engine. With only sizing binding, every path equals the fixed-fractional recursion; the default
profile keeps the 95th-percentile drawdown of a losing strategy inside the halt, a profile
without its throttle does not, a reckless one hits the halt and ruins; the calendar's cooldown
refuses entries; results are reproducible."""

from typing import Any

import numpy as np
import pandas as pd
import pytest

from helpers.quality import repo_config
from xq.backtest.costs import CostModel
from xq.core.config import RiskConfig
from xq.risk.engine import RiskEngine
from xq.robustness.montecarlo import TradeOutcomes, monte_carlo, trade_outcomes

CFG = repo_config()
GATES = CFG.gates_config()
DEFAULT = CFG.risk_config()
SETTINGS = CFG.validation_config().monte_carlo


def engine(profile: RiskConfig) -> RiskEngine:
    return RiskEngine(
        profile,
        CFG.instrument("xauusd"),
        margin_rate=CFG.backtest_config().event_config().margin_rate,
        costs=CostModel.from_config(CFG, "xauusd"),
    )


def profile(
    sizing: dict[str, Any] | None = None, limits: dict[str, Any] | None = None
) -> RiskConfig:
    return DEFAULT.model_copy(
        update={
            "sizing": DEFAULT.sizing.model_copy(update=sizing or {}),
            "limits": DEFAULT.limits.model_copy(update=limits or {}),
        }
    )


#: Only the 0.5 % risk budget binds: no throttle, no halt, no cooldown, no daily cap.
SIZING_ONLY = profile(
    {"throttle_start": 0.98, "throttle_end": 0.99},
    {
        "max_daily_loss": 0.99,
        "max_drawdown": 0.99,
        "max_consecutive_losses": 10_000,
        "max_trades_per_day": 10_000,
    },
)
#: 3 % of equity per trade and no throttle, but the default 15 % drawdown halt.
RECKLESS = profile({"risk_per_trade": 0.03, "throttle_start": 0.98, "throttle_end": 0.99})


def calendar(n: int, *, every: str = "1D", seed: int = 0) -> pd.DataFrame:
    """`n` trades entered at 14:00 UTC and closed 4 hours later, one per `every`."""
    start = pd.Timestamp("2022-01-03 14:00", tz="UTC")
    step = pd.Timedelta(every)
    entries = [start + i * step for i in range(n)]
    entries = [t + pd.Timedelta(days=2) if t.weekday() >= 5 else t for t in entries]
    return pd.DataFrame(
        {
            "entry_time": entries,
            "exit_time": [t + pd.Timedelta(hours=4) for t in entries],
            "direction": np.random.default_rng(seed).choice([-1, 1], n),
            "entry_price": 2000.0,
            "spread": 0.3,
            "sigma_daily": 0.01,
        }
    )


def outcomes(r: np.ndarray, *, every: str = "1D") -> TradeOutcomes:
    frame = calendar(len(r), every=every).assign(r_multiple=r)
    return TradeOutcomes(frame, SETTINGS.stop_sigmas)


def test_with_only_sizing_binding_every_path_is_the_fixed_fractional_recursion() -> None:
    r = np.random.default_rng(1).normal(0.05, 1.0, 200)
    trades = outcomes(r)
    result = monte_carlo(
        trades, engine(SIZING_ONLY), capital=1e7, n_paths=20, seed=3, ruin_level=0.5
    )
    risk = SIZING_ONLY.sizing.risk_per_trade
    for path, index in zip(result.paths.itertuples(), result.indices, strict=True):
        equity = np.cumprod(1 + risk * r[index])  # E_k+1 = E_k (1 + 0.5 % R_k)
        peak = np.maximum(np.maximum.accumulate(equity), 1.0)
        # lots round down to 0.01 of about 8.3: at most 0.12 % of each trade's risk
        assert path.final_equity == pytest.approx(equity[-1], rel=2e-3)
        assert path.max_drawdown == pytest.approx(np.max(1 - equity / peak), abs=2e-3)
        assert (path.taken, path.refused, path.halted, path.ruined) == (200, 0, False, False)


def test_the_default_profile_keeps_a_losing_strategy_inside_the_halt() -> None:
    # a strategy losing 0.1 R a trade after costs: its unmanaged drawdown runs far past 15 %
    trades = outcomes(np.random.default_rng(2).normal(-0.1, 1.0, 300))
    managed = monte_carlo(trades, engine(DEFAULT), capital=1e5, n_paths=100, seed=4, ruin_level=0.5)
    unmanaged = monte_carlo(
        trades, engine(SIZING_ONLY), capital=1e5, n_paths=100, seed=4, ruin_level=0.5
    )
    check = managed.gate_check(GATES)
    assert check.criterion.key == "monte_carlo_drawdown.below"
    assert check.passed  # the throttle shrinks the size towards zero before the halt
    assert managed.drawdown_quantile(0.95) < DEFAULT.limits.max_drawdown
    assert managed.halt_probability == 0.0
    assert managed.ruin_probability == 0.0
    assert managed.paths["refused"].mean() > 0  # sized to zero lots once deep in the throttle
    assert unmanaged.drawdown_quantile(0.95) > 0.2  # the same outcomes without the rules
    assert not unmanaged.gate_check(GATES).passed


def test_a_reckless_profile_hits_the_halt_and_breaks_the_budget() -> None:
    trades = outcomes(np.random.default_rng(2).normal(-0.1, 1.0, 300))
    result = monte_carlo(trades, engine(RECKLESS), capital=1e5, n_paths=100, seed=4, ruin_level=0.5)
    assert result.halt_probability > 0.9  # the halt fires ...
    assert result.drawdown_quantile(0.95) >= 0.15  # ... but only after the crossing trade
    assert not result.gate_check(GATES).passed
    assert result.paths.loc[result.paths["halted"], "refused"].min() > 0  # sticky: no new entry
    no_halt = profile(
        {"risk_per_trade": 0.03, "throttle_start": 0.98, "throttle_end": 0.99},
        {"max_drawdown": 0.99, "max_daily_loss": 0.99},
    )
    ruin = monte_carlo(trades, engine(no_halt), capital=1e5, n_paths=100, seed=4, ruin_level=0.5)
    assert ruin.ruin_probability > 0.5  # without the halt, 3 % a trade ruins most paths


def test_the_calendar_makes_the_cooldown_bind() -> None:
    # hourly trades that all lose 1 R: after five losing round trips, four hours of cooldown
    trades = outcomes(np.full(12, -1.0), every="1h")
    result = monte_carlo(trades, engine(DEFAULT), capital=1e5, n_paths=1, seed=0, ruin_level=0.5)
    path = result.paths.iloc[0]
    assert path["taken"] == 5
    assert path["refused"] == 7  # every later entry falls within four hours of the last loss


def test_trade_outcomes_measure_r_against_the_stop_and_inputs_are_checked() -> None:
    frame = calendar(3).assign(trade_return=[0.03, -0.015, 0.0])
    made = trade_outcomes(frame, stop_sigmas=3.0)
    np.testing.assert_allclose(made.r, [1.0, -0.5, 0.0])  # a 3 % move = 3 sigma-hats = 1 R
    first = monte_carlo(made, engine(DEFAULT), capital=1e5, n_paths=5, seed=9, ruin_level=0.5)
    second = monte_carlo(made, engine(DEFAULT), capital=1e5, n_paths=5, seed=9, ruin_level=0.5)
    pd.testing.assert_frame_equal(first.paths, second.paths)
    assert "halt_probability" in first.summary().index
    with pytest.raises(ValueError, match="at least one closed trade"):
        TradeOutcomes(made.frame.iloc[:0], 3.0)
    with pytest.raises(ValueError, match="positive"):
        TradeOutcomes(made.frame.assign(sigma_daily=0.0), 3.0)
    with pytest.raises(ValueError, match="ruin_level"):
        monte_carlo(made, engine(DEFAULT), capital=1e5, n_paths=5, seed=9, ruin_level=1.5)
