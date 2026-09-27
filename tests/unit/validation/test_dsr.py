"""VAL-002: PSR and DSR against the published example, Monte Carlo and a pure-noise family."""

import math

import numpy as np
import pytest
from scipy.stats import norm

from xq.validation.dsr import (
    deflated_sharpe,
    deflated_sharpe_of_returns,
    expected_max_sharpe,
    probabilistic_sharpe,
)
from xq.validation.sharpe import min_track_record_length, sharpe_ratio

RNG = np.random.default_rng(21)


def test_the_published_deflated_sharpe_example() -> None:
    """Bailey and Lopez de Prado (2014), section 4: annualized SR 2.5 over five years of daily
    returns (T = 1250), N = 100 trials with annualized Sharpe variance 0.5, skewness -3,
    kurtosis 10: the expected maximum is 0.1132 (daily) and DSR = 0.9004."""
    result = deflated_sharpe(
        2.5 / math.sqrt(250), 1250, -3.0, 10.0, n_trials=100, sharpe_variance=0.5 / 250
    )
    assert result.benchmark == pytest.approx(0.1132, abs=1e-4)
    assert result.dsr == pytest.approx(0.9004, abs=1e-4)


def test_psr_is_consistent_with_the_minimum_track_record_length() -> None:
    n = min_track_record_length(0.08, -0.5, 5.0, alpha=0.05)
    assert probabilistic_sharpe(0.08, n, -0.5, 5.0) == pytest.approx(0.95)
    assert probabilistic_sharpe(0.0, 500, 0.0, 3.0) == pytest.approx(0.5)
    assert probabilistic_sharpe(0.1, 500, 0.0, 3.0, sr_benchmark=0.1) == pytest.approx(0.5)
    assert math.isnan(probabilistic_sharpe(0.1, 1, 0.0, 3.0))


def test_expected_maximum_approximates_the_monte_carlo_maximum() -> None:
    draws = RNG.normal(0, 1, size=(20_000, 100)).max(axis=1)
    assert expected_max_sharpe(100, 1.0) == pytest.approx(draws.mean(), rel=0.02)
    assert expected_max_sharpe(1, 1.0) == 0.0  # one trial: nothing to deflate
    assert expected_max_sharpe(50, 0.0) == 0.0
    assert expected_max_sharpe(1000, 1.0) > expected_max_sharpe(100, 1.0)


def test_dsr_rejects_at_the_nominal_rate_on_a_pure_noise_family() -> None:
    """20 strategies without skill; the best one is selected. The DSR with the family's trial count
    and Sharpe variance exceeds 0.95 in about 5 % of families at most, while the naive PSR of the
    selected strategy would pass far more often."""
    families, n_trials, n = 400, 20, 500
    dsr_passes = psr_passes = 0
    for _ in range(families):
        returns = RNG.normal(0, 0.01, size=(n_trials, n))
        sharpes = np.array([sharpe_ratio(r) for r in returns])
        best = returns[int(np.argmax(sharpes))]
        result = deflated_sharpe_of_returns(
            best, n_trials=n_trials, sharpe_variance=float(np.var(sharpes, ddof=1))
        )
        dsr_passes += result.dsr > 0.95
        psr_passes += probabilistic_sharpe(result.sharpe, n, result.skew, result.kurtosis) > 0.95
    assert dsr_passes / families <= 0.08
    assert psr_passes / families > 0.3  # why deflation is needed


def test_dsr_fields() -> None:
    returns = RNG.normal(0.001, 0.01, 750)
    result = deflated_sharpe_of_returns(returns, n_trials=10, sharpe_variance=0.001)
    assert result.n == 750
    assert result.benchmark == pytest.approx(expected_max_sharpe(10, 0.001))
    z = (
        (result.sharpe - result.benchmark)
        * math.sqrt(749)
        / math.sqrt(1 - result.skew * result.sharpe + (result.kurtosis - 1) / 4 * result.sharpe**2)
    )
    assert result.dsr == pytest.approx(norm.cdf(z))
