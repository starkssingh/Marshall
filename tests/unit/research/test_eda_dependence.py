"""EDA-003: autocorrelations match statsmodels, recover an AR(1), and the robust bands are wider
than the i.i.d. ones under volatility clustering (GARCH) but not for i.i.d. data."""

import math

import numpy as np
import pytest
from statsmodels.tsa.stattools import acf as sm_acf
from statsmodels.tsa.stattools import pacf as sm_pacf

from helpers.simulate import ar1, garch_returns
from xq.research.eda.dependence import (
    SERIES,
    autocorrelation,
    dependence_figure,
    dependence_summary,
    dependence_table,
    lags_for,
    partial_autocorrelation,
    robust_se,
)


def test_acf_and_pacf_match_statsmodels() -> None:
    x = np.random.default_rng(1).standard_t(5, size=3000)
    ours = autocorrelation(x, 40)
    reference = sm_acf(x, nlags=40, adjusted=False, fft=False)[1:]
    np.testing.assert_allclose(ours, reference, atol=1e-12)
    np.testing.assert_allclose(
        partial_autocorrelation(ours), sm_pacf(x, nlags=40, method="ldb")[1:], atol=1e-10
    )


def test_ar1_autocorrelations_are_recovered() -> None:
    phi = 0.4
    x = ar1(100_000, phi, seed=2)
    rho = autocorrelation(x, 6)
    np.testing.assert_allclose(rho, phi ** np.arange(1, 7), atol=0.015)
    pacf = partial_autocorrelation(rho)
    assert pacf[0] == pytest.approx(phi, abs=0.015)
    assert np.all(np.abs(pacf[1:]) < 0.015)


def test_robust_band_equals_the_iid_band_for_iid_data() -> None:
    x = np.random.default_rng(3).standard_normal(100_000)
    ratio = robust_se(x, 10) * math.sqrt(len(x))
    assert np.all((ratio > 0.95) & (ratio < 1.05))


def test_robust_band_is_wider_than_iid_under_garch() -> None:
    # GARCH(1,1) alpha 0.15, beta 0.8: kurtosis about 5.6 and first autocorrelation of squared
    # returns about 0.3, so the robust band at lag 1 is about sqrt(1 + 0.3 * 4.6) = 1.5 times the
    # i.i.d. band.
    r = garch_returns(100_000, alpha=0.15, beta=0.8, seed=4)
    ratio = robust_se(r, 20) * math.sqrt(len(r))
    assert ratio[0] > 1.3
    assert np.mean(ratio[:5]) > 1.2
    table = dependence_table(r, 20, 0.95)
    returns = table.loc[table["series"] == "returns"]
    assert np.all(returns["robust_band"] >= returns["iid_band"])
    naive = int((np.abs(returns["acf"]) > returns["iid_band"]).sum())
    assert int(returns["significant"].sum()) <= naive
    squared = table.loc[table["series"] == "squared_returns"]
    assert bool(squared["significant"].iloc[0])  # volatility clustering is flagged
    assert squared["acf"].iloc[0] > 0.2


def test_dependence_table_layout_and_summary() -> None:
    r = np.random.default_rng(5).standard_normal(2000)
    table = dependence_table(r, 15, 0.95)
    assert list(table["series"].unique()) == list(SERIES)
    assert len(table) == 3 * 15
    np.testing.assert_allclose(table["iid_band"], 1.959963984540054 / math.sqrt(2000))
    abs_rows = table.loc[table["series"] == "abs_returns", "acf"].to_numpy()
    np.testing.assert_allclose(abs_rows, autocorrelation(np.abs(r), 15))
    summary = dependence_summary(table)
    assert summary["lags"].tolist() == [15, 15, 15]
    assert len(dependence_figure(table, "t").axes) == 4


def test_lag_count_is_one_trading_day_bounded_by_the_series() -> None:
    assert lags_for(1380, 20, 1_000_000) == 1380
    assert lags_for(1, 20, 1000) == 20
    assert lags_for(92, 20, 50) == 49
    assert lags_for(92, 20, 1) == 0
    with pytest.raises(ValueError, match="nlags"):
        autocorrelation(np.ones(5), 5)


def test_constant_series_has_zero_acf_and_undefined_bands() -> None:
    assert np.all(autocorrelation(np.ones(10), 3) == 0)
    assert np.all(np.isnan(robust_se(np.ones(10), 3)))
