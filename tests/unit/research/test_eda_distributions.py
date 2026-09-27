"""EDA-002: moments, normality, tails and bootstrap intervals, checked against scipy and against
simulated processes with known properties."""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from helpers.simulate import ar1, garch_returns
from xq.research.eda.bootstrap import (
    bootstrap_intervals,
    eda_block_length,
    stationary_resample,
)
from xq.research.eda.distributions import (
    MOMENTS,
    distribution_row,
    fit_student_t,
    hill_tail_index,
    jarque_bera,
    moments,
    qq_figure,
    tail_indices,
    yearly_moments,
)


def test_moments_match_scipy() -> None:
    x = np.random.default_rng(1).gamma(2.0, size=5000)
    result = moments(x)
    assert result["mean"] == pytest.approx(np.mean(x), rel=1e-12)
    assert result["std"] == pytest.approx(np.std(x, ddof=1), rel=1e-12)
    assert result["skew"] == pytest.approx(stats.skew(x), rel=1e-10)
    assert result["excess_kurtosis"] == pytest.approx(stats.kurtosis(x), rel=1e-10)


def test_moments_of_degenerate_series() -> None:
    assert all(math.isnan(v) for v in moments([1.0]).values())
    constant = moments([2.0, 2.0, 2.0])
    assert constant["std"] == 0.0
    assert math.isnan(constant["skew"])


def test_jarque_bera_matches_scipy() -> None:
    x = np.random.default_rng(2).standard_t(4, size=3000)
    statistic, p = jarque_bera(x)
    reference = stats.jarque_bera(x)
    assert statistic == pytest.approx(reference.statistic, rel=1e-12)
    assert p == pytest.approx(reference.pvalue, rel=1e-9, abs=1e-300)
    assert jarque_bera(np.random.default_rng(3).standard_normal(3000))[1] > 0.01
    assert p < 1e-10


def test_hill_recovers_a_pareto_tail_index() -> None:
    alpha = 3.0
    u = np.random.default_rng(4).random(200_000)
    pareto = u ** (-1 / alpha)
    assert hill_tail_index(pareto, 10_000) == pytest.approx(alpha, rel=0.05)
    assert math.isnan(hill_tail_index(pareto[:5], 10))


def test_tail_indices_of_a_student_t_and_a_normal() -> None:
    rng = np.random.default_rng(5)
    left, right = tail_indices(rng.standard_t(3, size=400_000), 0.02)
    assert 2.4 < left < 3.8
    assert 2.4 < right < 3.8
    normal_left, _ = tail_indices(rng.standard_normal(400_000), 0.02)
    assert normal_left > 5  # a thin tail: no finite power-law index


def test_student_t_fit_recovers_degrees_of_freedom() -> None:
    x = 2.0 + 0.5 * np.random.default_rng(6).standard_t(5, size=20_000)
    df, loc, scale = fit_student_t(x)
    assert df == pytest.approx(5, rel=0.2)
    assert loc == pytest.approx(2.0, abs=0.02)
    assert scale == pytest.approx(0.5, rel=0.05)


def test_stationary_resample_has_geometric_blocks() -> None:
    rng = np.random.default_rng(7)
    index = stationary_resample(rng, 100_000, 20.0)
    assert len(index) == 100_000
    assert index.min() >= 0
    assert index.max() < 100_000
    continues = np.mean(np.diff(index) % 100_000 == 1)
    assert continues == pytest.approx(1 - 1 / 20, abs=0.01)
    again = stationary_resample(np.random.default_rng(7), 100_000, 20.0)
    np.testing.assert_array_equal(index, again)
    with pytest.raises(ValueError, match="mean_block"):
        stationary_resample(rng, 10, 0.5)


def test_bootstrap_interval_of_a_mean_covers_it_with_the_right_width() -> None:
    x = np.random.default_rng(8).standard_normal(4000)
    intervals = bootstrap_intervals(x, moments, n_boot=400, mean_block=1.0, level=0.95, seed=1)
    low, high = intervals["mean"]
    assert low < 0 < high
    assert high - low == pytest.approx(2 * 1.96 / math.sqrt(4000), rel=0.2)
    assert set(intervals) == set(MOMENTS)
    assert intervals == bootstrap_intervals(
        x, moments, n_boot=400, mean_block=1.0, level=0.95, seed=1
    )


def test_block_bootstrap_widens_the_interval_of_a_dependent_series() -> None:
    # AR(1) with phi 0.5: the long-run variance of the mean is (1 + phi) / (1 - phi) = 3 times
    # the i.i.d. one, so the interval should be about sqrt(3) wider with blocks than without.
    x = ar1(20_000, 0.5, seed=9)
    iid = bootstrap_intervals(x, moments, n_boot=300, mean_block=1.0, level=0.95, seed=2)
    block = bootstrap_intervals(x, moments, n_boot=300, mean_block=50.0, level=0.95, seed=2)
    ratio = (block["mean"][1] - block["mean"][0]) / (iid["mean"][1] - iid["mean"][0])
    assert 1.4 < ratio < 2.1


def test_block_length_floor_and_cap() -> None:
    garch = garch_returns(50_000, alpha=0.15, beta=0.8, seed=10)
    assert eda_block_length(garch, 5) > 5  # volatility clustering lengthens the blocks
    assert eda_block_length(garch, 1000) == 1000.0
    assert eda_block_length(garch[:2000], 1000) == 200.0  # at most a tenth of the series
    assert eda_block_length([1.0, 2.0], 5) == 1.0


def test_distribution_row_is_in_basis_points() -> None:
    x = np.random.default_rng(11).standard_t(5, size=5000) * 1e-4
    row = distribution_row(x, hill_fraction=0.05, n_boot=200, mean_block=10.0, level=0.9, seed=3)
    assert row["n"] == 5000
    assert row["std"] == pytest.approx(np.std(x * 1e4, ddof=1))
    assert row["mean_ci_low"] < row["mean"] < row["mean_ci_high"]
    assert row["excess_kurtosis_ci_low"] < row["excess_kurtosis"] < row["excess_kurtosis_ci_high"]
    assert row["block_length"] == 10.0
    assert row["jb_p"] < 1e-6
    assert row["t_df"] == pytest.approx(5, rel=0.35)


def test_yearly_moments_group_by_the_trading_days_year() -> None:
    frame = pd.DataFrame(
        {
            "trading_day": [date(2022, 12, 30), date(2023, 1, 2), date(2023, 1, 3)],
            "ret": [0.001, -0.002, 0.003],
        }
    )
    years = yearly_moments(frame)
    assert years["year"].tolist() == [2022, 2023]
    assert years["n"].tolist() == [1, 2]
    assert years.loc[1, "mean"] == pytest.approx(5.0)


def test_qq_figure_renders() -> None:
    x = np.random.default_rng(12).standard_normal(10_000) * 1e-4
    figure = qq_figure(x, fit_student_t(x * 1e4), "test")
    assert len(figure.axes) == 2
