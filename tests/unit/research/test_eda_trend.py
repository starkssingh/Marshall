"""EDA-005: variance ratios against their AR(1) values, sign runs and drawdown episodes."""

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from helpers.simulate import ar1, garch_returns
from xq.research.eda.trend import (
    MAX_RUN_BUCKET,
    drawdown_episodes,
    drawdown_summary,
    run_length_counts,
    sign_runs,
    underwater_figure,
    variance_ratio,
    variance_ratio_figure,
    variance_ratio_table,
)


def theoretical_vr(phi: float, q: int) -> float:
    k = np.arange(1, q)
    return 1 + 2 * float(np.sum((1 - k / q) * phi**k))


def reference_vr(r: np.ndarray, q: int) -> float:
    """Lo-MacKinlay overlapping estimator written out term by term."""
    n = len(r)
    mu = r.mean()
    sigma_1 = np.sum((r - mu) ** 2) / (n - 1)
    sums = [np.sum(r[t - q + 1 : t + 1]) - q * mu for t in range(q - 1, n)]
    m = q * (n - q + 1) * (1 - q / n)
    return float(np.sum(np.square(sums)) / m / sigma_1)


def test_variance_ratio_matches_the_estimator_written_out() -> None:
    r = np.random.default_rng(1).standard_normal(500)
    for q in (2, 5, 17):
        assert variance_ratio(r, q)[0] == pytest.approx(reference_vr(r, q), rel=1e-12)


@pytest.mark.parametrize("phi", [0.3, -0.3])
def test_variance_ratio_of_an_ar1_matches_theory(phi: float) -> None:
    r = ar1(200_000, phi, seed=2)
    for q in (2, 4, 10):
        ratio, z = variance_ratio(r, q)
        assert ratio == pytest.approx(theoretical_vr(phi, q), abs=0.02)
        assert (z > 10) if phi > 0 else (z < -10)


def test_variance_ratio_of_heteroskedastic_noise_is_near_one() -> None:
    r = garch_returns(100_000, alpha=0.15, beta=0.8, seed=3)
    table = variance_ratio_table(r, [2, 8, 32])
    assert np.all(np.abs(table["variance_ratio"] - 1) < 0.05)
    assert np.all(np.abs(table["z_robust"]) < 3)
    assert math.isnan(variance_ratio(r[:5], 10)[0])


def test_sign_runs_count_and_expectation() -> None:
    result = sign_runs([1.0, 2.0, 0.0, -1.0, 3.0, -2.0, -1.0])  # + + (0) - + - -
    assert result["n"] == 6
    assert result["runs"] == 4
    up, down, n = 3, 3, 6
    assert result["expected_runs"] == pytest.approx(2 * up * down / n + 1)
    assert result["mean_run_length"] == pytest.approx(1.5)
    assert result["share_up"] == pytest.approx(0.5)


def test_sign_runs_describe_persistence_and_reversal() -> None:
    assert sign_runs(ar1(50_000, 0.5, seed=4))["z"] < -10
    assert sign_runs(ar1(50_000, -0.5, seed=5))["z"] > 10
    assert abs(sign_runs(np.random.default_rng(6).standard_normal(50_000))["z"]) < 3.5


def test_run_length_counts_match_independent_signs() -> None:
    x = np.random.default_rng(7).standard_normal(100_000)
    counts = run_length_counts(x)
    assert len(counts) == MAX_RUN_BUCKET
    assert counts["observed"].sum() == sign_runs(x)["runs"]
    assert counts["expected"].sum() == pytest.approx(counts["observed"].sum(), rel=1e-6)
    np.testing.assert_allclose(counts["observed"][:4], counts["expected"][:4], rtol=0.05)


def prices(values: list[float]) -> pd.Series:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(len(values))]
    return pd.Series(values, index=days)


def test_drawdown_episodes_of_a_known_path() -> None:
    path = prices([100, 110, 99, 105, 111, 90, 95])
    episodes = drawdown_episodes(path)
    assert len(episodes) == 2
    first, second = episodes.iloc[0], episodes.iloc[1]
    assert (first["peak"], first["trough"], first["recovery"]) == (
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 5),
    )
    assert first["depth"] == pytest.approx(0.1)
    assert (first["days_to_trough"], first["days_underwater"], first["recovered"]) == (1, 2, True)
    assert second["recovery"] is None
    assert second["depth"] == pytest.approx(1 - 90 / 111)
    assert (second["days_underwater"], second["recovered"]) == (2, False)
    summary = drawdown_summary(path)
    assert summary["max_drawdown"] == pytest.approx(1 - 90 / 111)
    assert summary["share_underwater"] == pytest.approx(4 / 7)
    assert summary["longest_underwater_days"] == 2
    assert summary["episodes"] == 2


def test_rising_path_has_no_drawdown() -> None:
    path = prices([1.0, 2.0, 3.0])
    assert drawdown_episodes(path).empty
    assert drawdown_summary(path)["max_drawdown"] == 0.0
    assert math.isnan(drawdown_summary(prices([]))["max_drawdown"])


def test_trend_figures_render() -> None:
    path = prices([100, 110, 99, 105, 111, 90, 95])
    assert len(underwater_figure(path, "t").axes) == 2
    table = variance_ratio_table(np.random.default_rng(8).standard_normal(1000), [2, 4])
    assert len(variance_ratio_figure(table, "t").axes) == 1
