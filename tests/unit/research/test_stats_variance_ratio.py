"""STAT-003 recovery: on the increments of an Ornstein-Uhlenbeck process the variance ratio is
below 1 (and matches its theory) and the Chow-Denning joint test rejects; on a random walk it does
not reject, with near-nominal size over many draws; slices find reversion only where it is."""

import numpy as np
import pandas as pd
import pytest

from helpers.simulate import garch_returns, ornstein_uhlenbeck, random_walk
from xq.core.config import VarianceRatioConfig
from xq.research.eda.trend import variance_ratio
from xq.research.stats.variance_ratio import (
    segmented_variance_ratio,
    variance_ratio_by_slice,
    variance_ratio_tests,
    volatility_regime_labels,
)

HORIZONS = [2, 4, 8, 16, 32, 64]
ALPHA = 0.05


def test_ou_increments_have_variance_ratios_below_one_matching_theory() -> None:
    theta = 0.05
    x = ornstein_uhlenbeck(100_000, theta=theta, seed=4)
    tests = variance_ratio_tests(np.diff(x), "ou", HORIZONS, ALPHA)
    phi = np.exp(-theta)
    for result in tests.horizons:
        q = result.lags
        assert q is not None
        expected = (1 - phi**q) / (q * (1 - phi))  # Var(x_{t+q} - x_t) / (q Var(x_{t+1} - x_t))
        assert result.details["variance_ratio"] < 1
        assert result.details["variance_ratio"] == pytest.approx(expected, abs=0.03)
    assert tests.joint.reject
    assert tests.joint.statistic == max(abs(r.statistic) for r in tests.horizons)


def test_random_walk_increments_do_not_reject() -> None:
    tests = variance_ratio_tests(np.diff(random_walk(20_000, seed=3)), "rw", HORIZONS, ALPHA)
    assert not tests.joint.reject
    ratios = [r.details["variance_ratio"] for r in tests.horizons]
    np.testing.assert_allclose(ratios, 1.0, atol=0.1)


def test_chow_denning_size_is_controlled_on_garch_returns() -> None:
    rejections = [
        variance_ratio_tests(
            garch_returns(2000, alpha=0.1, beta=0.85, seed=500 + s), "g", HORIZONS, ALPHA
        ).joint.reject
        for s in range(100)
    ]
    assert np.mean(rejections) <= 0.10


def test_one_segment_equals_the_eda_variance_ratio() -> None:
    r = garch_returns(3000, alpha=0.1, beta=0.8, seed=2)
    for q in (2, 16):
        assert segmented_variance_ratio(r, None, q) == pytest.approx(variance_ratio(r, q))
        assert segmented_variance_ratio(r, np.zeros(len(r)), q) == pytest.approx(
            variance_ratio(r, q)
        )


def test_windows_never_cross_segments() -> None:
    rng = np.random.default_rng(0)
    a, b = rng.standard_normal(500), rng.standard_normal(500) + 100.0  # a jump between segments
    joined = np.r_[a, b]
    ids = np.r_[np.zeros(500), np.ones(500)]
    ratio, _ = segmented_variance_ratio(joined - np.r_[np.zeros(500), np.full(500, 100.0)], ids, 4)
    assert ratio == pytest.approx(1.0, abs=0.15)
    with pytest.raises(ValueError, match="contiguous"):
        segmented_variance_ratio(joined, np.r_[np.zeros(300), np.ones(400), np.zeros(300)], 4)


def test_slices_find_reversion_only_where_it_is() -> None:
    rng = np.random.default_rng(12)
    blocks, labels = [], []
    for k in range(200):  # alternating 100-bar blocks: reverting "A", i.i.d. "B"
        if k % 2 == 0:
            blocks.append(np.diff(ornstein_uhlenbeck(101, theta=0.3, seed=k)))
            labels.extend(["A"] * 100)
        else:
            blocks.append(rng.standard_normal(100) * 0.8)
            labels.extend(["B"] * 100)
    index = pd.date_range("2022-01-03", periods=20_000, freq="15min", tz="UTC")
    returns = pd.Series(np.concatenate(blocks), index=index)
    cfg = VarianceRatioConfig(horizons=[2, 4, 8], regime_window=20, regime_quantiles=[0.5])
    table = variance_ratio_by_slice(returns, pd.Series(labels, index=index), cfg, ALPHA, name="x")
    joint = table.loc[table["test"] == "Chow-Denning"].set_index("slice")
    assert joint.loc["A", "p_holm_slices"] < ALPHA
    assert joint.loc["B", "p_holm_slices"] >= ALPHA
    vr_a = table.loc[(table["slice"] == "A") & (table["test"] == "VR(4)"), "detail_variance_ratio"]
    assert float(vr_a.iloc[0]) < 0.9


def test_regime_labels_use_only_earlier_returns_and_reference_cut_offs() -> None:
    r = pd.Series(garch_returns(3000, alpha=0.1, beta=0.85, seed=7))
    reference = np.arange(3000) < 1500
    labels = volatility_regime_labels(r, 50, [1 / 3, 2 / 3], reference=reference)
    assert labels.iloc[:50].isna().all()
    assert set(labels.dropna()) == {"v0", "v1", "v2"}
    changed = r.copy()
    changed.iloc[2000:] *= 10  # later data cannot move earlier labels or the reference cut-offs
    relabelled = volatility_regime_labels(changed, 50, [1 / 3, 2 / 3], reference=reference)
    pd.testing.assert_series_equal(labels.iloc[:2001], relabelled.iloc[:2001])
    assert (relabelled.iloc[2051:] == "v2").mean() > 0.9
