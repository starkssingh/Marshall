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


def _ar1_series(seed: int) -> dict[str, np.ndarray]:
    """The five series the Politis-White reference values below were computed on."""
    rng = np.random.default_rng(seed)
    series = {}
    for name, phi, n in [
        ("iid_500", 0.0, 500),
        ("ar05_1000", 0.5, 1000),
        ("ar_neg03_750", -0.3, 750),
        ("ar08_2000", 0.8, 2000),
        ("ar02_300", 0.2, 300),
    ]:
        e = rng.standard_normal(n)
        series[name] = lfilter([1.0], [1.0, -phi], e)
    return series


# `arch.bootstrap.optimal_block_length(x)["stationary"]` (arch 8.0.0, an independent
# implementation of Patton, Politis and White 2009) on the series of `_ar1_series(20260927)`.
ARCH_STATIONARY_BLOCK = {
    "iid_500": 0.6864018015538239,
    "ar05_1000": 11.487021433177038,
    "ar_neg03_750": 7.002654652308571,
    "ar08_2000": 30.096931677306305,
    "ar02_300": 2.292148018389538,
}


def test_politis_white_matches_an_independent_implementation() -> None:
    for name, x in _ar1_series(20260927).items():
        assert s.politis_white_block_length(x) == pytest.approx(
            ARCH_STATIONARY_BLOCK[name], rel=1e-9
        ), name


def test_politis_white_recovers_the_ar1_optimum() -> None:
    # For AR(1) with coefficient phi the optimal stationary-bootstrap block length is
    # (2 phi / (1 - phi^2))^(2/3) n^(1/3) (G / g(0) = 2 phi / (1 - phi^2)).
    n, phi = 5000, 0.5
    theory = (2 * phi / (1 - phi**2)) ** (2 / 3) * n ** (1 / 3)
    rng = np.random.default_rng(3)
    blocks = [
        s.politis_white_block_length(lfilter([1.0], [1.0, -phi], rng.standard_normal(n)))
        for _ in range(40)
    ]
    assert np.mean(blocks) == pytest.approx(theory, rel=0.15)


def test_politis_white_edge_cases_and_gate_floor() -> None:
    assert s.politis_white_block_length([0.01, 0.02]) == 1.0
    assert s.politis_white_block_length(np.full(100, 0.01)) == 1.0
    noise = np.random.default_rng(4).standard_normal(500)
    assert s.politis_white_block_length(noise) < 5
    assert s.gate_block_length(noise, "politis_white", 5) == 5.0
    assert s.gate_block_length(noise, 8, 5) == 8.0
    trending = lfilter([1.0], [1.0, -0.9], np.random.default_rng(5).standard_normal(2000))
    assert s.gate_block_length(trending, "politis_white", 5) > 5
    # capped at ceil(min(3 sqrt(n), n / 3))
    assert s.politis_white_block_length(np.cumsum(np.ones(90))) <= 29


def test_bootstrap_sharpe_p_value_and_interval() -> None:
    rng = np.random.default_rng(6)
    strong = rng.normal(0.002, 0.01, 750)  # daily Sharpe 0.2, annualized ~3.2
    result = s.bootstrap_sharpe(strong, n_boot=2000, mean_block=5, seed=1)
    assert result.p_value < 0.001
    assert result.ci_low < result.sharpe < result.ci_high
    assert result.ci_low > 0
    assert result == s.bootstrap_sharpe(strong, n_boot=2000, mean_block=5, seed=1)
    losing = s.bootstrap_sharpe(-strong, n_boot=2000, mean_block=5, seed=1)
    assert losing.p_value > 0.99
    empty = s.bootstrap_sharpe([0.01, 0.01, 0.01], n_boot=100, mean_block=5, seed=1)
    assert math.isnan(empty.p_value)


def test_bootstrap_sharpe_test_has_nominal_size_under_the_null() -> None:
    # Zero-mean AR(1) returns: the one-sided 5 % test rejects about 5 % of the time.
    rng = np.random.default_rng(7)
    rejections = 0
    reps = 300
    for rep in range(reps):
        x = lfilter([1.0], [1.0, -0.3], rng.standard_normal(500)) * 0.01
        block = s.gate_block_length(x, "politis_white", 5)
        p = s.bootstrap_sharpe(x, n_boot=499, mean_block=block, seed=rep).p_value
        rejections += p < 0.05
    assert 0.02 <= rejections / reps <= 0.09


def test_bootstrap_distribution_is_chunked_deterministically() -> None:
    x = np.random.default_rng(8).normal(0, 1, 300)
    a = s.bootstrap_distribution(x, s.row_sharpe, n_boot=1234, mean_block=4, seed=2, chunk=500)
    b = s.bootstrap_distribution(x, s.row_sharpe, n_boot=1234, mean_block=4, seed=2, chunk=500)
    assert a.shape == (1234,)
    np.testing.assert_array_equal(a, b)
    means = s.bootstrap_distribution(x, lambda d: d.mean(axis=1), n_boot=2000, mean_block=1, seed=3)
    assert means.std() == pytest.approx(x.std() / math.sqrt(300), rel=0.1)
