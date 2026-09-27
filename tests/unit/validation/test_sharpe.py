"""VAL-001: Sharpe standard errors, annualization, bootstrap interval and MinTRL.

Closed forms are checked by hand; the standard errors are checked by Monte Carlo against the
sampling distribution they describe (normal, skewed and autocorrelated returns)."""

import math
from collections.abc import Callable

import numpy as np
import pytest
from scipy.signal import lfilter
from scipy.stats import norm

from xq.validation import sharpe as s

RNG = np.random.default_rng(12)


def simulated_sharpes(draw: Callable[[], np.ndarray], reps: int = 3000) -> np.ndarray:
    return np.array([s.sharpe_ratio(draw()) for _ in range(reps)])


def test_closed_forms_by_hand() -> None:
    assert s.se_iid(0.1, 250) == pytest.approx(math.sqrt(1.005 / 250))
    assert s.se_non_normal(0.1, 250, 0.0, 3.0) == pytest.approx(s.se_iid(0.1, 250))
    # negative skew and fat tails widen the error
    assert s.se_non_normal(0.1, 250, -1.0, 8.0) == pytest.approx(
        math.sqrt((1.005 + 0.1 + 0.0125) / 250)
    )
    assert s.sharpe_ratio([0.01, 0.03, -0.01]) == pytest.approx(0.01 / 0.02)
    assert math.isnan(s.sharpe_ratio([0.01, 0.01]))


def test_iid_normal_standard_error_matches_the_sampling_distribution() -> None:
    n = 250
    draws = simulated_sharpes(lambda: RNG.normal(0.001, 0.01, n))
    assert draws.std() == pytest.approx(s.se_iid(0.1, n), rel=0.05)
    sample = RNG.normal(0.001, 0.01, n)
    assert s.se_hac(sample, max_lag=0) == pytest.approx(s.se_iid(0.1, n), rel=0.15)


def test_non_normal_standard_error_tracks_skewed_returns() -> None:
    n = 500

    def skewed() -> np.ndarray:  # small gains, rare large losses: negative skew, fat tails
        crash = RNG.random(n) < 0.03
        return np.where(crash, -0.06, 0.004) + RNG.normal(0, 0.005, n)

    draws = simulated_sharpes(skewed)
    pooled = np.concatenate([skewed() for _ in range(50)])
    sr = s.sharpe_ratio(pooled)
    skewness, kurt = s.moments(pooled)
    assert skewness < -2
    assert kurt > 6
    mertens = s.se_non_normal(sr, n, skewness, kurt)
    assert draws.std() == pytest.approx(mertens, rel=0.1)
    assert abs(draws.std() - s.se_iid(sr, n)) > abs(draws.std() - mertens)


def test_hac_standard_error_tracks_autocorrelated_returns() -> None:
    n, phi = 1000, 0.3

    def ar1() -> np.ndarray:
        x = lfilter([1.0], [1.0, -phi], RNG.normal(0, 0.01, n + 50))
        return 0.001 + np.asarray(x)[50:]

    draws = simulated_sharpes(ar1, reps=1500)
    hac = np.mean([s.se_hac(ar1(), max_lag=10) for _ in range(100)])
    assert draws.std() == pytest.approx(hac, rel=0.15)
    assert draws.std() > 1.2 * s.se_iid(float(np.mean(draws)), n)  # i.i.d. understates it


def test_lo_annualization_factor() -> None:
    assert s.eta_for_autocorrelations(np.zeros(11), 12) == pytest.approx(math.sqrt(12))
    rho, q = 0.5, 12
    powers = rho ** np.arange(1, q)
    closed = rho * (q * (1 - rho) - (1 - rho**q)) / (1 - rho) ** 2
    assert s.eta_for_autocorrelations(powers, q) == pytest.approx(q / math.sqrt(q + 2 * closed))
    assert s.eta_for_autocorrelations(powers, q) < math.sqrt(
        q
    )  # positive autocorrelation shrinks it
    x = lfilter([1.0], [1.0, -rho], RNG.normal(size=40_000))
    assert s.annualization_factor(x, q, max_lag=11) == pytest.approx(
        s.eta_for_autocorrelations(powers, q), rel=0.03
    )


def test_stationary_bootstrap_blocks_and_interval_coverage() -> None:
    index = s.stationary_bootstrap(1000, n_boot=50, mean_block=10, seed=1)
    continues = np.diff(index, axis=1) == 1
    assert 1 / (1 - continues.mean()) == pytest.approx(10, rel=0.1)
    np.testing.assert_array_equal(
        index, s.stationary_bootstrap(1000, n_boot=50, mean_block=10, seed=1)
    )
    covered = 0
    reps = 200
    for rep in range(reps):
        sample = RNG.normal(0.001, 0.01, 300)
        low, high = s.bootstrap_ci(sample, n_boot=400, mean_block=3, seed=rep)
        covered += low <= 0.1 <= high
    assert 0.88 <= covered / reps <= 0.99


def test_min_track_record_length() -> None:
    n = s.min_track_record_length(0.1, 0.0, 3.0)
    assert n == pytest.approx(1 + 1.005 * (norm.ppf(0.95) / 0.1) ** 2)
    # at exactly that length the observed Sharpe ratio is significant at 5 %
    z = 0.1 * math.sqrt(n - 1) / math.sqrt(1 + (3 - 1) / 4 * 0.01)
    assert z == pytest.approx(norm.ppf(0.95))
    assert s.min_track_record_length(0.1, -1.0, 8.0) > n  # skew and tails demand more history
    assert s.min_track_record_length(0.05, 0.0, 3.0, sr_benchmark=0.1) == math.inf


def test_estimate_bundles_everything() -> None:
    sample = RNG.normal(0.001, 0.01, 500)
    result = s.estimate(sample)
    assert result.n == 500
    assert result.sharpe == pytest.approx(s.sharpe_ratio(sample))
    assert result.se_iid == pytest.approx(s.se_iid(result.sharpe, 500))
    assert result.kurtosis == pytest.approx(3, abs=0.6)
    with pytest.raises(ValueError, match="missing"):
        s.estimate([0.1, float("nan")])
