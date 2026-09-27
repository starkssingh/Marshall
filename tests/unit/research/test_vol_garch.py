"""VOL-004 recovery: GARCH(1,1), GJR-GARCH and EGARCH parameters are recovered within tolerance
on simulated data; Student-t degrees of freedom too; forecasts match the GARCH recursion and its
multi-step closed form; forecasts are causal and simulated ones reproducible."""

import math

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from helpers.simulate import garch_periods
from xq.core.config import GarchSpec, load_config
from xq.research.volatility.garch import GarchForecaster, garch_forecasters

GARCH = GarchSpec(vol="GARCH", o=0)
GJR = GarchSpec(vol="GARCH", o=1)
EGARCH = GarchSpec(vol="EGARCH", o=1)


def test_garch11_parameters_are_recovered() -> None:
    periods, _ = garch_periods(6000, omega=0.05, alpha=0.08, beta=0.90, seed=1)
    model = GarchForecaster("garch_normal", GARCH, "normal").fit(periods)
    params = model.diagnostics.params
    assert model.diagnostics.converged
    assert params["alpha[1]"] == pytest.approx(0.08, abs=0.025)
    assert params["beta[1]"] == pytest.approx(0.90, abs=0.03)
    unconditional = params["omega"] * model.scale**2 / (1 - params["alpha[1]"] - params["beta[1]"])
    assert unconditional == pytest.approx(0.05 / 0.02, rel=0.25)
    assert model.diagnostics.persistence == pytest.approx(0.98, abs=0.02)


def test_gjr_leverage_is_recovered() -> None:
    periods, _ = garch_periods(8000, omega=0.05, alpha=0.03, beta=0.88, gamma=0.12, seed=2)
    params = GarchForecaster("gjr_normal", GJR, "normal").fit(periods).diagnostics.params
    assert params["gamma[1]"] == pytest.approx(0.12, abs=0.04)
    assert params["alpha[1]"] == pytest.approx(0.03, abs=0.03)
    assert params["beta[1]"] == pytest.approx(0.88, abs=0.03)


def _egarch_periods(n: int, seed: int) -> pd.DataFrame:
    """EGARCH(1,1,1) as arch defines it: ln h = w + a (|e| - E|e|) + g e + b ln h_{-1}."""
    rng = np.random.default_rng(seed)
    w, a, g, b = -0.01, 0.15, -0.08, 0.97
    e = rng.standard_normal(n + 500)
    log_h = np.empty(n + 500)
    log_h[0] = w / (1 - b)
    for t in range(1, n + 500):
        log_h[t] = w + a * (abs(e[t - 1]) - math.sqrt(2 / math.pi)) + g * e[t - 1]
        log_h[t] += b * log_h[t - 1]
    ret = np.exp(log_h / 2) * e
    index = pd.date_range("2000-01-03 22:00", periods=n, freq="D", tz="UTC", name="decision_time")
    return pd.DataFrame(
        {"period_start": index - pd.Timedelta(days=1), "ret": ret[500:], "rv": ret[500:] ** 2},
        index=index,
    )


def test_egarch_parameters_are_recovered() -> None:
    model = GarchForecaster("egarch_normal", EGARCH, "normal").fit(_egarch_periods(8000, seed=3))
    params = model.diagnostics.params
    assert params["alpha[1]"] == pytest.approx(0.15, abs=0.04)
    assert params["gamma[1]"] == pytest.approx(-0.08, abs=0.03)
    assert params["beta[1]"] == pytest.approx(0.97, abs=0.02)


def test_student_t_degrees_of_freedom_are_recovered() -> None:
    rng = np.random.default_rng(4)
    n, nu = 8000, 8.0
    z = rng.standard_t(nu, n + 500) / math.sqrt(nu / (nu - 2))
    h, r = np.empty(n + 500), np.empty(n + 500)
    h[0] = 1.0
    for t in range(n + 500):
        if t:
            h[t] = 0.02 + 0.08 * r[t - 1] ** 2 + 0.9 * h[t - 1]
        r[t] = math.sqrt(h[t]) * z[t]
    index = pd.date_range("2000-01-03 22:00", periods=n, freq="D", tz="UTC", name="decision_time")
    periods = pd.DataFrame(
        {"period_start": index - pd.Timedelta(days=1), "ret": r[500:], "rv": r[500:] ** 2},
        index=index,
    )
    params = GarchForecaster("garch_t", GARCH, "t").fit(periods).diagnostics.params
    assert params["nu"] == pytest.approx(nu, rel=0.3)


def test_forecasts_follow_the_garch_recursion_and_its_closed_form() -> None:
    periods, _ = garch_periods(3000, omega=0.05, alpha=0.08, beta=0.90, seed=5)
    model = GarchForecaster("garch_normal", GARCH, "normal").fit(periods.iloc[:2000])
    p = model.diagnostics.params
    omega, alpha, beta = p["omega"], p["alpha[1]"], p["beta[1]"]
    one = model.predict_variance(periods, 1).to_numpy() / model.scale**2  # h_{t+1} in fit units
    r = periods["ret"].to_numpy() / model.scale
    t = np.arange(100, 2999)
    np.testing.assert_allclose(one[t + 1], omega + alpha * r[t + 1] ** 2 + beta * one[t], rtol=1e-9)
    five = model.predict_variance(periods, 5).to_numpy() / model.scale**2
    persistence = alpha + beta
    level = omega / (1 - persistence)
    expected = sum(level + persistence**k * (one - level) for k in range(5))
    np.testing.assert_allclose(five[100:], expected[100:], rtol=1e-9)


@pytest.mark.parametrize(
    ("spec", "distribution", "horizon"),
    [(GARCH, "normal", 5), (GJR, "skewt", 1), (EGARCH, "t", 3)],
)
def test_forecasts_are_causal_and_reproducible(
    spec: GarchSpec, distribution: str, horizon: int
) -> None:
    periods, _ = garch_periods(500, omega=0.05, alpha=0.08, beta=0.90, seed=6)
    model = GarchForecaster("m", spec, distribution, simulations=200, seed=7).fit(
        periods.iloc[:300]
    )
    base = model.predict_variance(periods, horizon)
    changed = periods.copy()
    changed.iloc[400:, changed.columns.get_indexer(["ret"])] *= 5.0
    after = model.predict_variance(changed, horizon)
    pd.testing.assert_series_equal(base.iloc[75:400], after.iloc[75:400])
    assert not np.allclose(base.iloc[401:], after.iloc[401:])
    pd.testing.assert_series_equal(base, model.predict_variance(periods, horizon))
    assert (base.iloc[75:] > 0).all()


def test_the_configured_family() -> None:
    cfg = load_config("dev", config_dir=REPO / "config").volatility_config().garch
    names = list(garch_forecasters(cfg, seed=1))
    assert len(names) == 9
    assert {"garch_normal", "gjr_skewt", "egarch_t"} <= set(names)
    model = garch_forecasters(cfg, seed=1)["egarch_skewt"]()
    assert isinstance(model, GarchForecaster)
    assert model.name == "egarch_skewt"
    with pytest.raises(ValueError, match="distribution"):
        GarchForecaster("x", GARCH, "cauchy")
    with pytest.raises(RuntimeError, match="fit"):
        GarchForecaster("x", GARCH, "normal").predict_variance(
            garch_periods(10, omega=0.05, alpha=0.08, beta=0.9, seed=1)[0], 1
        )
