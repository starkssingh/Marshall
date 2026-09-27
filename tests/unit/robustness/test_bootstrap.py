"""ROB-003: stationary block bootstrap of daily returns and trade-order permutation — interval
coverage close to nominal for Sharpe and CAGR (iid, volatility clustering, serial correlation,
where an iid bootstrap under-covers), the drawdown interval against the true drawdown median, and
the trade-order percentile uniform for unordered trades but extreme for clustered losses."""

from collections.abc import Callable

import numpy as np
import pytest

from helpers.simulate import ar1, garch_returns
from xq.robustness.bootstrap import bootstrap_returns, permute_trades, return_statistics

P = 252
N = 750
SIGMA = 0.01
LEVEL = 0.90
REPLICATIONS = 200  # Monte Carlo standard error of a 90 % coverage: 2.1 points


def coverage(
    make: Callable[[int], np.ndarray], true_sharpe: float, **kwargs: float
) -> tuple[float, float]:
    mu = true_sharpe / np.sqrt(P) * SIGMA
    true_cagr = (1 + N * mu) ** (P / N) - 1  # equity is capital plus cumulative P&L
    sharpe = cagr = 0
    for seed in range(REPLICATIONS):
        result = bootstrap_returns(
            mu + SIGMA * make(seed),
            periods_per_year=P,
            n_boot=499,
            seed=10**6 + seed,
            level=LEVEL,
            **kwargs,
        )
        sharpe += result.sharpe.covers(true_sharpe)
        cagr += result.cagr.covers(true_cagr)
    return sharpe / REPLICATIONS, cagr / REPLICATIONS


@pytest.mark.parametrize(
    "make",
    [
        lambda seed: np.random.default_rng(seed).standard_normal(N),
        lambda seed: garch_returns(N, alpha=0.08, beta=0.9, seed=seed),
    ],
    ids=["iid", "garch"],
)
def test_sharpe_and_cagr_intervals_cover_at_about_the_nominal_rate(
    make: Callable[[int], np.ndarray],
) -> None:
    for rate in coverage(make, true_sharpe=1.0):
        assert 0.85 <= rate <= 0.95


def test_blocks_keep_the_coverage_under_serial_correlation() -> None:
    phi = 0.3

    def make(seed: int) -> np.ndarray:
        return ar1(N, phi, seed=seed) * np.sqrt(1 - phi**2)  # unit variance

    blocks = coverage(make, true_sharpe=1.0)
    iid = coverage(make, true_sharpe=1.0, mean_block=1.0)
    for rate in blocks:
        assert 0.85 <= rate <= 0.95
    for with_blocks, without in zip(blocks, iid, strict=True):
        assert without < 0.85  # resampling single days ignores the dependence ...
        assert with_blocks - without > 0.05  # ... and the Politis-White blocks restore it


def test_the_drawdown_interval_brackets_the_true_drawdown_median() -> None:
    mu = 1.0 / np.sqrt(P) * SIGMA
    fresh = mu + SIGMA * np.random.default_rng(99).standard_normal((5_000, N))
    true_median = float(np.median(return_statistics(fresh, P)[:, 2]))
    covered, medians = 0, []
    for seed in range(REPLICATIONS):
        returns = mu + SIGMA * np.random.default_rng(seed).standard_normal(N)
        result = bootstrap_returns(returns, periods_per_year=P, n_boot=499, seed=seed, level=LEVEL)
        covered += result.max_drawdown.covers(true_median)
        medians.append(result.draws["max_drawdown"].median())
    assert 0.85 <= covered / REPLICATIONS <= 0.99
    assert np.mean(medians) == pytest.approx(true_median, rel=0.15)


def test_statistics_follow_the_backtest_definitions() -> None:
    returns = np.array([-0.01, 0.02, -0.03, 0.01])
    sharpe, cagr, drawdown = return_statistics(returns, 4)[0]
    assert sharpe == pytest.approx(np.mean(returns) / np.std(returns, ddof=1) * 2)
    assert cagr == pytest.approx((1 + returns.sum()) ** (4 / 4) - 1)
    # equity 0.99, 1.01, 0.98, 0.99 from 1: the fall from 1.01 to 0.98 is the largest
    assert drawdown == pytest.approx(0.03 / 1.01)
    assert return_statistics([-0.01, -0.01, -0.01], 252)[0, 2] == pytest.approx(0.03)  # from 1
    assert np.isnan(return_statistics([-0.6, -0.6, 0.1], 252)[0, 1])  # everything lost
    result = bootstrap_returns(
        np.random.default_rng(0).normal(0.001, 0.01, 300), periods_per_year=P, n_boot=200, seed=1
    )
    assert result.mean_block >= 5  # the gates' minimum block
    assert list(result.table().index) == ["sharpe", "cagr", "max_drawdown"]
    assert result.sharpe.low < result.sharpe.estimate < result.sharpe.high
    again = bootstrap_returns(
        np.random.default_rng(0).normal(0.001, 0.01, 300), periods_per_year=P, n_boot=200, seed=1
    )
    assert again.draws.equals(result.draws)
    with pytest.raises(ValueError, match="missing"):
        bootstrap_returns([0.1, np.nan, 0.2, 0.1], periods_per_year=P, n_boot=10, seed=1)


def test_unordered_trades_sit_anywhere_in_the_permutation_distribution() -> None:
    percentiles = np.array(
        [
            permute_trades(
                np.random.default_rng(seed).normal(50, 1000, 200),
                capital=100_000,
                n_perm=999,
                seed=seed,
            ).percentile("max_drawdown")
            for seed in range(300)
        ]
    )
    assert np.mean(percentiles > 0.95) == pytest.approx(0.05, abs=0.035)
    assert np.mean(percentiles < 0.05) == pytest.approx(0.05, abs=0.035)
    assert np.mean(percentiles) == pytest.approx(0.5, abs=0.05)


def test_clustered_losses_are_at_the_top_of_the_permutation_distribution() -> None:
    pnl = np.sort(np.random.default_rng(1).normal(50, 1000, 200))  # every loss first
    result = permute_trades(pnl, capital=100_000, n_perm=999, seed=1)
    assert result.percentile("max_drawdown") == 1.0
    assert result.percentile("under_water") == 1.0
    summary = result.summary()
    assert summary.loc["max_drawdown", "observed"] > summary.loc["max_drawdown", "q0.95"]
    assert result.observed_under_water == 200  # below the starting capital from the first loss
    with pytest.raises(ValueError, match="at least one trade"):
        permute_trades([], capital=1.0, n_perm=10, seed=1)
