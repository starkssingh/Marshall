"""STAT-006 recovery: an AR(1) with phi = 0.5 is recovered; forecasts match the closed form and
statsmodels' one-step predictions, and are causal; in walk-forward an AR(1) beats the zero-return
and random-walk forecasts by Diebold-Mariano after Holm across horizons, i.i.d. returns do not, and
every model runs on identical folds."""

import numpy as np
import pandas as pd
import pytest
from statsmodels.tsa.arima.model import ARIMA

from helpers.pipeline import REPO
from helpers.simulate import ar1, bar_frame, completed_block_columns, forward_target
from xq.core.config import ArmaSpec, load_config
from xq.research.stats.arima import (
    ARMA_SPEC,
    ArmaFit,
    HorizonTarget,
    arma_forecasts,
    arma_model_config,
    arma_study,
    fit_arma,
    select_ar,
)
from xq.validation.splitters import WalkForwardConfig
from xq.validation.walkforward import walk_forward

SPLITTER = WalkForwardConfig(mode="expanding", min_train="40D", test_len="20D", embargo="0D")


def test_ar1_with_phi_one_half_is_recovered() -> None:
    fit = fit_arma(0.001 * ar1(5000, 0.5, seed=1) + 0.0002, 1)
    assert fit.ar[0] == pytest.approx(0.5, abs=0.03)
    assert abs(fit.ar[0] - 0.5) < 3 * fit.se["ar.L1"]
    assert fit.mu == pytest.approx(0.0002, abs=0.0001)
    assert fit.sigma2 == pytest.approx(1e-6, rel=0.05)


def test_arma11_parameters_are_recovered() -> None:
    rng = np.random.default_rng(3)
    e = rng.standard_normal(8000)
    x = np.zeros(8000)
    for t in range(1, 8000):
        x[t] = 0.5 * x[t - 1] + e[t] + 0.3 * e[t - 1]
    fit = fit_arma(x, 1, 1)
    assert fit.ar[0] == pytest.approx(0.5, abs=0.05)
    assert fit.ma[0] == pytest.approx(0.3, abs=0.05)


def test_aic_selects_the_order_of_an_ar2() -> None:
    rng = np.random.default_rng(5)
    e = rng.standard_normal(6000)
    x = np.zeros(6000)
    for t in range(2, 6000):
        x[t] = 0.3 * x[t - 1] + 0.3 * x[t - 2] + e[t]
    assert select_ar(x, 4).p == 2


def test_ar1_forecasts_match_the_closed_form() -> None:
    fit = ArmaFit(p=1, q=0, mu=0.1, ar=(0.5,), ma=(), sigma2=1.0, aic=0.0, nobs=100)
    r = np.random.default_rng(0).standard_normal(50)
    for h in (1, 3):
        expected = h * 0.1 + sum(0.5**k for k in range(1, h + 1)) * (r - 0.1)
        np.testing.assert_allclose(arma_forecasts(fit, r, h), expected)


def test_one_step_forecasts_match_statsmodels_after_burn_in() -> None:
    rng = np.random.default_rng(8)
    e = rng.standard_normal(3000)
    x = np.zeros(3000)
    for t in range(1, 3000):
        x[t] = 0.4 * x[t - 1] + e[t] + 0.3 * e[t - 1]
    result = ARIMA(x, order=(1, 0, 1), trend="c").fit()
    const, phi, theta, sigma2 = (float(v) for v in result.params)
    fit = ArmaFit(p=1, q=1, mu=const, ar=(phi,), ma=(theta,), sigma2=sigma2, aic=0.0, nobs=3000)
    ours = arma_forecasts(fit, x, 1)[:-1]  # made at t for t + 1
    reference = np.asarray(result.predict())[1:]  # statsmodels' prediction of t + 1
    np.testing.assert_allclose(ours[100:], reference[100:], atol=1e-8)


def test_forecasts_are_causal() -> None:
    fit = ArmaFit(p=2, q=1, mu=0.0, ar=(0.3, 0.2), ma=(0.4,), sigma2=1.0, aic=0.0, nobs=100)
    r = np.random.default_rng(2).standard_normal(300)
    base = arma_forecasts(fit, r, 4)
    changed = r.copy()
    changed[150:] += 5.0
    np.testing.assert_array_equal(arma_forecasts(fit, changed, 4)[:150], base[:150])
    assert not np.allclose(arma_forecasts(fit, changed, 4)[150:], base[150:])


def _study_inputs(returns: np.ndarray) -> tuple[pd.DataFrame, list[HorizonTarget]]:
    frame = bar_frame(returns)
    frame = frame.join(completed_block_columns(frame, 4, "ctx_1h_"))
    horizons = []
    for label, bars, columns in (
        ("15m", 1, ("open", "close")),
        ("1h", 4, ("ctx_1h_open", "ctx_1h_close")),
    ):
        y, ends = forward_target(frame, bars)
        horizons.append(HorizonTarget(label, y, ends, bars, columns))
    return frame, horizons


MODELS = {"ar1": ArmaSpec(p=1), "arma11": ArmaSpec(p=1, q=1)}


def test_walk_forward_ar1_beats_the_benchmarks_on_identical_folds() -> None:
    frame, horizons = _study_inputs(0.001 * ar1(9600, 0.2, seed=4))
    study = arma_study(
        frame, horizons, MODELS, ["zero_return", "random_walk"], SPLITTER, alpha=0.05, seed=1
    )
    assert study.useful("ar1")
    assert study.useful("arma11")
    table = study.comparisons
    assert set(table["benchmark"]) == {"zero_return", "random_walk"}
    assert (table.loc[table["model"] == "ar1", "p_holm"] < 0.05).all()
    assert (table["p_holm"] >= table["p_one_sided"] - 1e-15).all()
    for label in ("15m", "1h"):
        assert len(study.fold_ids[label]) >= 3
        predictions = study.predictions[label]
        assert {"ar1", "arma11", "zero_return", "random_walk"} <= set(predictions.columns)
    assert study.in_sample["ar1"].ar[0] == pytest.approx(0.2, abs=0.05)


def test_iid_returns_give_no_useful_evidence() -> None:
    returns = 0.001 * np.random.default_rng(6).standard_normal(9600)
    frame, horizons = _study_inputs(returns)
    study = arma_study(
        frame, horizons, {"ar1": ArmaSpec(p=1)}, ["zero_return"], SPLITTER, alpha=0.05, seed=1
    )
    assert not study.useful("ar1")


def test_the_estimator_runs_in_the_walk_forward_runner() -> None:
    frame = bar_frame(0.001 * ar1(4000, 0.5, seed=9))
    y, ends = forward_target(frame, 1)
    output = walk_forward(
        frame, y, ends, ARMA_SPEC, arma_model_config("ar1", ArmaSpec(p=1), 1), SPLITTER, seed=3
    )
    predictions = output.predictions.dropna(subset=["y_true"])
    corr = np.corrcoef(predictions["y_pred"], predictions["y_true"])[0, 1]
    assert corr == pytest.approx(0.5, abs=0.06)


def test_the_committed_models_are_valid() -> None:
    arima = load_config("dev", config_dir=REPO / "config").stats_config().arima
    assert set(arima.models) == {"ar1", "arma11", "ar_aic"}
    assert arima.models["ar_aic"].max_p == 5
    with pytest.raises(ValueError, match="max_p only"):
        ArmaSpec(p=1, max_p=3)
    with pytest.raises(ValueError, match="p \\+ q >= 1"):
        ArmaSpec()
