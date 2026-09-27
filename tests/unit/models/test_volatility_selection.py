"""VOL-006: the selection defaults to EWMA when nothing beats it, promotes only a model in the MCS
that beats EWMA by Diebold-Mariano, and the selected forecaster serves sigma-hat per fold, fitted
on each fold's training periods only."""

import math

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from helpers.simulate import garch_periods
from xq.core.config import load_config
from xq.core.types import Timeframe
from xq.models.volatility import Selection, select_forecaster, serve_sigma
from xq.research.volatility.benchmarks import Deseasonalized, Ewma, benchmark_forecasters
from xq.research.volatility.evaluate import board_forecasters, evaluate_forecasters
from xq.research.volatility.garch import garch_forecasters
from xq.validation.forecast_eval import diebold_mariano_less, model_confidence_set
from xq.validation.splitters import WalkForwardConfig

DEFAULT = "ewma_0.94"


def _metrics(rows: list[tuple[str, float, bool, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["model", "qlike", "in_mcs", "dm_vs_default_p_less"])


def test_nothing_beats_the_default_so_ewma_stays() -> None:
    metrics = _metrics(
        [
            (DEFAULT, 0.30, True, math.nan),
            ("har", 0.29, True, 0.20),  # lower QLIKE, but not significantly better
            ("garch_t", 0.25, False, 0.01),  # significant, but outside the MCS
            ("rolling_22", 0.40, False, 0.99),
        ]
    )
    selection = select_forecaster(metrics, default=DEFAULT, dm_alpha=0.05)
    assert selection == Selection(DEFAULT, DEFAULT, False, (), selection.reasons, selection.p_holm)
    assert "does not beat" in selection.reasons["har"]
    assert "not in the model confidence set" in selection.reasons["garch_t"]


def test_only_the_default_on_the_board_keeps_the_default() -> None:
    selection = select_forecaster(
        _metrics([(DEFAULT, 0.3, True, math.nan)]), default=DEFAULT, dm_alpha=0.05
    )
    assert selection.selected == DEFAULT
    assert not selection.beats_default


def test_the_best_eligible_model_is_selected() -> None:
    metrics = _metrics(
        [
            (DEFAULT, 0.30, False, math.nan),
            ("har", 0.27, True, 0.01),
            ("gjr_t", 0.26, True, 0.03),
            ("egarch_t", 0.20, False, 0.001),  # best QLIKE but outside the MCS
        ]
    )
    selection = select_forecaster(metrics, default=DEFAULT, dm_alpha=0.05)
    assert selection.selected == "gjr_t"
    assert selection.beats_default
    assert selection.eligible == ("har", "gjr_t")
    assert "selected" in selection.reasons["gjr_t"]


def test_p_values_are_holm_adjusted_across_all_challengers() -> None:
    metrics = _metrics(
        [
            (DEFAULT, 0.30, True, math.nan),
            ("har", 0.27, True, 0.02),  # below 0.05 alone, not after Holm across three
            ("gjr_t", 0.28, True, 0.30),
            ("egarch_t", 0.29, False, 0.60),  # outside the MCS, but still in the Holm family
        ]
    )
    selection = select_forecaster(metrics, default=DEFAULT, dm_alpha=0.05)
    assert selection.selected == DEFAULT
    assert selection.p_holm == pytest.approx({"har": 0.06, "gjr_t": 0.6, "egarch_t": 0.6})
    assert "after Holm across 3 challengers" in selection.reasons["har"]


def test_twelve_null_challengers_promote_in_at_most_about_five_percent_of_samples() -> None:
    """Twelve challengers whose expected QLIKE equals the default's: promotions are false."""
    rng = np.random.default_rng(20260927)
    challengers = [f"m{k:02d}" for k in range(12)]
    n_sims, n_obs = 400, 500
    promoted = naive = 0
    for sim in range(n_sims):
        common = rng.gamma(2.0, 0.5, n_obs)  # the shared difficulty of each period
        default_loss = common + rng.normal(0.0, 0.3, n_obs)
        losses = {DEFAULT: default_loss}
        for name in challengers:
            losses[name] = common + rng.normal(0.0, 0.3, n_obs)
        frame = pd.DataFrame(losses)
        mcs = model_confidence_set(frame, alpha=0.10, n_boot=200, mean_block=5.0, seed=sim)
        rows = [(DEFAULT, float(default_loss.mean()), DEFAULT in mcs.included, math.nan)]
        for name in challengers:
            p = diebold_mariano_less(losses[name], default_loss).p_value
            rows.append((name, float(losses[name].mean()), name in mcs.included, p))
        metrics = _metrics(rows)
        promoted += select_forecaster(metrics, default=DEFAULT, dm_alpha=0.05).beats_default
        naive += bool(  # the rule without the Holm adjustment
            ((metrics["dm_vs_default_p_less"] < 0.05) & metrics["in_mcs"]).any()
        )
    assert promoted / n_sims <= 0.07  # at most about dm_alpha (binomial error ~1 %)
    assert naive / n_sims >= 0.25  # about 40 %: why the adjustment is needed


def test_the_default_must_be_on_the_board() -> None:
    with pytest.raises(ValueError, match="not on the board"):
        select_forecaster(_metrics([("har", 0.3, True, 0.01)]), default=DEFAULT, dm_alpha=0.05)


def _riskmetrics_periods(n: int, seed: int) -> pd.DataFrame:
    """Returns whose variance follows the RiskMetrics recursion with lambda 0.94 (plus a drift)."""
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((n, 24)) / np.sqrt(24)
    s = np.empty(n)
    ret = np.empty(n)
    s[0] = 1.0
    for t in range(n):
        if t:
            s[t] = 0.002 + 0.94 * s[t - 1] + 0.06 * ret[t - 1] ** 2
        ret[t] = np.sqrt(s[t]) * z[t].sum()
    intra = np.sqrt(s)[:, None] * z
    index = pd.date_range("2010-01-04 22:00", periods=n, freq="D", tz="UTC", name="decision_time")
    return pd.DataFrame(
        {"period_start": index - pd.Timedelta(days=1), "ret": ret, "rv": (intra**2).sum(axis=1)},
        index=index,
    )


def test_ewma_is_kept_end_to_end_when_it_is_the_true_model() -> None:
    cfg = load_config("dev", config_dir=REPO / "config").volatility_config()
    factories = benchmark_forecasters(cfg, Timeframe("1d"))
    factories |= {
        k: v for k, v in garch_forecasters(cfg.garch, seed=1).items() if k.endswith("_normal")
    }
    board = evaluate_forecasters(
        _riskmetrics_periods(3000, seed=2),
        factories,
        1,
        WalkForwardConfig(min_train="1000D", test_len="365D"),
        cfg.evaluation,
        default=cfg.selection.default,
        seed=3,
    )
    selection = select_forecaster(
        board.metrics, default=cfg.selection.default, dm_alpha=cfg.evaluation.dm_alpha
    )
    assert selection.selected == "ewma_0.94"
    assert not selection.beats_default


def test_sigma_is_served_per_fold_from_training_periods_only() -> None:
    periods, _ = garch_periods(2000, omega=0.05, alpha=0.08, beta=0.9, seed=4)
    splitter = WalkForwardConfig(min_train="800D", test_len="300D")
    served = serve_sigma(periods, lambda: Ewma(0.94), 1, splitter)
    assert served["fold_id"].nunique() >= 3
    assert (served["train_end"] < served.index).all()
    first = served.loc[served["fold_id"] == "f000"]
    train = periods.loc[: first["train_end"].iloc[0]]
    manual = Ewma(0.94).fit(train).predict(periods.loc[: first.index[-1]], 1)
    np.testing.assert_allclose(first["sigma_hat"], manual.loc[first.index])
    changed = periods.copy()
    cut = served.index[len(served) // 2]
    changed.loc[changed.index > cut, ["ret", "rv"]] *= 10.0
    again = serve_sigma(changed, lambda: Ewma(0.94), 1, splitter)
    pd.testing.assert_frame_equal(served.loc[:cut], again.loc[:cut])


def test_the_volatility_board_holds_the_benchmarks_and_the_garch_family() -> None:
    cfg = load_config("dev", config_dir=REPO / "config").volatility_config()
    daily = board_forecasters(cfg, Timeframe("1d"), seed=1)
    assert list(daily)[:4] == ["rolling_22", "ewma_0.94", "ewma_0.97", "har"]  # BASE-003
    assert len(daily) == 4 + 9
    assert cfg.selection.default in daily
    assert cfg.evaluation.dm_reference in daily
    hourly = board_forecasters(cfg, Timeframe("1h"), seed=1)
    assert all(isinstance(make(), Deseasonalized) for make in hourly.values())
