"""ROB-007: execution-delay sensitivity — a genuine trend edge decays smoothly with the delay and
passes the R2 gate, a bid-ask-bounce edge flips sign at the first delay and fails it, a look-ahead
leak collapses (its too-good undelayed Sharpe is the tell), and the screener path delays every
order by whole decision bars."""

import numpy as np
import pandas as pd
import pytest

from helpers.event_backtest import CAPITAL, CFG, CLOCK, exact_costs, random_quotes
from helpers.simulate import ar1
from helpers.strategies import drift_returns, strategy_returns, trend_positions
from xq.backtest.vectorized import run_vectorized
from xq.robustness.delay import DelayCurve, delay_curve, delayed_positions, screen_delays

GATES = CFG.gates_config()
SEEDS = range(40)


def curve(returns: np.ndarray, positions: np.ndarray, cost: float = 0.0) -> DelayCurve:
    return delay_curve(
        lambda k: strategy_returns(returns, positions, cost=cost, delay=k), periods_per_year=252
    )


def test_a_genuine_trend_edge_decays_smoothly_and_passes_the_gate() -> None:
    curves = []
    for seed in SEEDS:
        returns = drift_returns(2500, seed=seed, drift_sigma=0.001, persistence=0.97)
        curves.append(curve(returns, trend_positions(returns, 15), cost=0.0002))
    retention = np.nanmedian([c.retention for c in curves], axis=0)
    assert np.all(np.diff(retention) < 0)  # every extra bar costs a little ...
    assert retention[-1] > 0.8  # ... and three bars keep most of the edge
    assert np.median([c.at(0) for c in curves]) < 3  # a plausible edge, not a leak
    assert sum(c.flips for c in curves) <= 2
    assert np.mean([c.gate_check(GATES).passed for c in curves]) >= 0.95


def test_a_bid_ask_bounce_edge_flips_at_the_first_delay() -> None:
    curves = []
    for seed in SEEDS:
        returns = 0.004 * ar1(2500, -0.3, seed=seed)  # negative autocorrelation: a bounce
        reversal = -np.sign(np.concatenate([[0.0], returns[:-1]]))
        curves.append(curve(returns, reversal))
    assert all(c.at(0) > 0 for c in curves)
    assert all(c.flips for c in curves)
    assert np.mean([c.gate_check(GATES).passed for c in curves]) <= 0.05


def test_a_look_ahead_leak_collapses_at_the_first_delay() -> None:
    curves = []
    for seed in SEEDS:
        returns = np.random.default_rng(seed).normal(0.0, 0.004, 2500)
        curves.append(curve(returns, np.sign(returns)))  # trades on the return it earns
    assert min(c.at(0) for c in curves) > 10  # far too good: the leakage tell (CLAUDE.md)
    assert np.median([c.retention[1] for c in curves]) < 0.02


def test_the_screener_path_delays_every_order_by_whole_bars() -> None:
    times = pd.date_range("2024-03-04 13:00", "2024-03-28 13:00", freq="3h", tz="UTC")
    rng = np.random.default_rng(4)
    positions = pd.Series(rng.choice([-1.0, 0.0, 1.0], len(times)), index=times)
    late = delayed_positions(positions, 2)
    assert late.iloc[:2].eq(0.0).all()
    np.testing.assert_array_equal(late.to_numpy()[2:], positions.to_numpy()[:-2])
    assert late.index.equals(positions.index)  # the same decision times, older targets
    quotes = random_quotes("2024-03-04", "2024-03-30", seed=4, every_s=60)
    costs = exact_costs()
    result = screen_delays(
        positions, quotes, costs, CLOCK, capital=CAPITAL, periods_per_year=252, delays=[1, 3]
    )
    assert result.delays == (0, 1, 3)  # the undelayed strategy is always included
    direct = run_vectorized(positions, quotes, costs, CLOCK, capital=CAPITAL)
    returns = direct.daily["return"]
    assert result.at(0) == pytest.approx(returns.mean() / returns.std() * np.sqrt(252))
    assert result.gate_check(GATES).criterion.key == "execution_delay.net_sharpe_min"
    assert list(result.table().columns) == ["sharpe", "retention", "net_return"]
    with pytest.raises(ValueError, match="not evaluated"):
        result.at(2)
    with pytest.raises(ValueError, match="negative"):
        delayed_positions(positions, -1)
