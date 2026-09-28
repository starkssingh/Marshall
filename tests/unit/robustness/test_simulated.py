"""The known-truth simulated strategies (ADR 0056): rebuilt exactly from their spec, the candidate
is the family's in-sample best, trades add up to the daily P&L, the re-evaluation functions agree
with the recorded returns, and the overfit family's configurations are independent noise."""

import math

import numpy as np
import pandas as pd
import pytest

from helpers.quality import repo_config
from xq.robustness.simulated import SimulationSpec, simulated_family_returns, simulated_subject
from xq.validation.sharpe import sharpe_ratio

GATES = repo_config().gates_config()


def subject(truth: str, seed: int = 0):  # type: ignore[no-untyped-def]
    return simulated_subject(
        SimulationSpec(truth=truth, seed=seed),  # type: ignore[arg-type]
        capital=100_000.0,
        periods_per_year=252,
    )


@pytest.mark.parametrize("truth", ["genuine", "overfit"])
def test_a_spec_rebuilds_the_same_strategy_and_its_candidate_is_the_best(truth: str) -> None:
    first, second = subject(truth), subject(truth)
    pd.testing.assert_series_equal(first.returns, second.returns)
    pd.testing.assert_frame_equal(first.trades, second.trades)
    sharpes = {c: sharpe_ratio(first.family[c].to_numpy()) for c in first.family.columns}
    assert first.name == max(sharpes, key=lambda c: sharpes[c])
    pd.testing.assert_frame_equal(
        simulated_family_returns(SimulationSpec(truth=truth, seed=0)),  # type: ignore[arg-type]
        first.family,
    )
    assert len(first.family.columns) == (24 if truth == "genuine" else 50)
    assert first.synthetic


@pytest.mark.parametrize("truth", ["genuine", "overfit"])
def test_re_evaluations_agree_with_the_recorded_returns(truth: str) -> None:
    s = subject(truth)
    values = {p.name: p.nominal for p in s.parameters}
    assert s.evaluate is not None
    np.testing.assert_allclose(s.evaluate(values), s.returns.to_numpy())
    np.testing.assert_allclose(s.delayed(0), s.returns.to_numpy())
    for evaluate in s.noisy.values():
        np.testing.assert_allclose(evaluate(0.0, 1), s.returns.to_numpy())  # no noise, no change
    table = s.cost_stress(GATES).table
    assert table.loc["baseline", "net_pnl"] == pytest.approx(float(s.returns.sum()))
    # every cost is linear in its multiplier on the synthetic asset
    spread = table.loc["spread_x1.25":"spread_x2", "net_pnl"].to_numpy()  # type: ignore[misc]
    assert np.allclose(np.diff(np.diff(np.r_[table.loc["baseline", "net_pnl"], spread[1:]])), 0)


def test_closed_trades_add_up_to_the_daily_pnl() -> None:
    s = subject("genuine")
    trades = s.trades
    assert (trades["exit_time"] > trades["entry_time"]).all()
    assert set(trades["direction"]) <= {-1, 1}
    assert (trades["sigma_daily"] > 0).all()
    # the trades' net P&L misses only the days outside closed trades (open at the end, flat days
    # pay nothing), the first days without a sigma-hat, and the timing of flip costs
    assert trades["pnl"].sum() == pytest.approx(s.daily_pnl.sum(), rel=0.05)


def test_the_overfit_family_is_independent_noise() -> None:
    s = subject("overfit")
    corr = s.family.corr().to_numpy()
    off_diagonal = corr[~np.eye(len(corr), dtype=bool)]
    # every configuration its own noise: spells of 5-14 days leave some sampling correlation
    assert np.abs(off_diagonal).mean() < 0.05
    assert np.abs(off_diagonal).max() < 0.25
    annual = np.array([sharpe_ratio(s.family[c].to_numpy()) for c in s.family]) * math.sqrt(252)
    assert np.median(annual) < 0.1  # no configuration has an edge after costs ...
    assert sharpe_ratio(s.returns.to_numpy()) * math.sqrt(252) > 0.2  # ... but the best looks good
