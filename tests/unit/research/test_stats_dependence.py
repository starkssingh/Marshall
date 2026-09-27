"""STAT-002 recovery: Ljung-Box and ARCH-LM match statsmodels; on a GARCH(1,1) simulation
Ljung-Box on squared returns and ARCH-LM reject; the robust portmanteau keeps its size on GARCH
returns (which are uncorrelated) where the plain Ljung-Box over-rejects; an AR(1) is detected."""

import numpy as np
import pytest
from statsmodels.stats.diagnostic import acorr_ljungbox, het_arch

from helpers.simulate import ar1, garch_returns
from xq.core.config import StatsDependenceConfig
from xq.research.stats.dependence import arch_lm, dependence_tests, ljung_box, robust_portmanteau
from xq.research.stats.results import holm_adjust

CFG = StatsDependenceConfig(ljung_box_lags=[1, 5, 10, 20], arch_lm_lags=[5, 10])
ALPHA = 0.05


def test_ljung_box_and_arch_lm_match_statsmodels() -> None:
    x = np.random.default_rng(4).standard_t(6, size=1500)
    reference = acorr_ljungbox(x, lags=[1, 5, 10], return_df=True)
    for lag in (1, 5, 10):
        q, p = ljung_box(x, lag)
        assert q == pytest.approx(reference.loc[lag, "lb_stat"], rel=1e-10)
        assert p == pytest.approx(reference.loc[lag, "lb_pvalue"], rel=1e-8)
    for lag in (1, 5):
        lm, p = arch_lm(x, lag)
        expected = het_arch(x - x.mean(), nlags=lag, result_object=True)  # on residuals
        assert lm == pytest.approx(expected.lm, rel=1e-8)
        assert p == pytest.approx(expected.lmpval, rel=1e-6)


def test_garch_squared_returns_reject_and_arch_lm_rejects() -> None:
    r = garch_returns(4000, alpha=0.1, beta=0.85, seed=21)
    tests = dependence_tests(r, "garch", CFG, ALPHA)
    assert tests.any_rejects("LB", "squared_returns")
    assert tests.any_rejects("LB", "abs_returns")
    assert tests.any_rejects("ARCH-LM")
    assert all(res.reject for res in tests.select("ARCH-LM"))


def test_iid_returns_show_no_arch_effects() -> None:
    r = np.random.default_rng(9).standard_normal(4000)
    tests = dependence_tests(r, "iid", CFG, ALPHA)
    assert not tests.any_rejects("ARCH-LM")
    assert not tests.any_rejects("LB", "squared_returns")
    assert not tests.any_rejects("Q*", "returns")


def test_robust_portmanteau_keeps_its_size_under_garch_where_ljung_box_does_not() -> None:
    plain, robust = [], []
    for seed in range(150):
        r = garch_returns(1500, alpha=0.2, beta=0.75, seed=1000 + seed)
        plain.append(ljung_box(r, 5)[1] < ALPHA)
        robust.append(robust_portmanteau(r, 5)[1] < ALPHA)
    assert np.mean(robust) <= 0.10
    assert np.mean(plain) > np.mean(robust)


def test_ar1_returns_are_detected_by_the_robust_test() -> None:
    tests = dependence_tests(ar1(3000, 0.2, seed=6), "ar1", CFG, ALPHA)
    assert tests.any_rejects("Q*", "returns")
    assert tests.any_rejects("LB", "returns")


def test_holm_adjustment() -> None:
    np.testing.assert_allclose(holm_adjust([0.01, 0.04, 0.03, 0.2]), [0.04, 0.09, 0.09, 0.2])
    adjusted = holm_adjust([0.02, np.nan, 0.5])
    assert np.isnan(adjusted[1])
    np.testing.assert_allclose(adjusted[[0, 2]], [0.04, 0.5])
    assert np.isnan(holm_adjust([np.nan])).all()


def test_results_carry_holm_p_values_and_the_table_lists_every_lag() -> None:
    tests = dependence_tests(np.random.default_rng(2).standard_normal(500), "x", CFG, ALPHA)
    table = tests.table()
    assert len(table) == 3 * 4 + 4 + 2
    assert (table["detail_p_holm"] >= table["p_value"] - 1e-15).all()
    assert set(table["test"]) == {"LB", "Q*", "ARCH-LM"}


def test_too_short_or_missing_values_are_refused() -> None:
    with pytest.raises(ValueError, match="too few"):
        ljung_box(np.arange(20.0), 10)
    with pytest.raises(ValueError, match="missing"):
        arch_lm(np.r_[np.nan, np.ones(100)], 1)
