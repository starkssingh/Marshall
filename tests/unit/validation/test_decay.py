"""R2 decay trend: the regression of walk-forward fold performance on time keeps its size on
stable edges (iid, and a genuine edge whose strength drifts in slow regimes, where a daily
Newey-West regression over-rejects), detects an edge fading away, and feeds the decay_trend
gate."""

import numpy as np
import pytest

from helpers.quality import repo_config
from xq.robustness.simulated import SimulationSpec, simulated_subject
from xq.validation.decay import decay_trend

GATES = repo_config().gates_config()


def folds(n: int, k: int = 10) -> np.ndarray:
    return np.arange(n) * k // n


def rejected(returns: np.ndarray, labels: np.ndarray) -> bool:
    return decay_trend(returns, labels, periods_per_year=252).p_value < 0.05


def test_a_stable_edge_is_rejected_at_about_the_nominal_rate() -> None:
    iid = np.mean(
        [
            rejected(np.random.default_rng(s).normal(0.0005, 0.01, 1000), folds(1000))
            for s in range(400)
        ]
    )
    assert 0.02 <= iid <= 0.08  # 400 replications: the standard error at 5 % is 1.1 points


def test_a_genuine_edge_with_regimes_keeps_the_size() -> None:
    # the simulated genuine trend edge: its strength drifts with a persistent AR(1) mean; a daily
    # Newey-West regression rejected it at 14 % (ADR 0056), the fold means do not
    rates = [
        rejected(s.returns.to_numpy(), s.folds.to_numpy())
        for s in (
            simulated_subject(
                SimulationSpec(truth="genuine", seed=seed), capital=1e5, periods_per_year=252
            )
            for seed in range(80)
        )
    ]
    assert np.mean(rates) <= 0.11  # 80 replications: nominal 5 % plus about 2.5 standard errors


def test_a_fading_edge_is_detected_and_fails_the_gate() -> None:
    drift = np.linspace(0.002, -0.001, 1000)  # 20 bp a day falling to -10 bp over four years
    rate = np.mean(
        [
            rejected(drift + np.random.default_rng(s).normal(0, 0.01, 1000), folds(1000))
            for s in range(200)
        ]
    )
    assert rate > 0.65
    trend = decay_trend(
        drift + np.random.default_rng(1).normal(0, 0.01, 1000), folds(1000), periods_per_year=252
    )
    assert trend.n_folds == 10
    assert trend.slope_per_year == pytest.approx(-0.003 / (999 / 252), rel=0.6)
    check = trend.gate_check(GATES)
    assert check.criterion.key == "decay_trend.significance"
    stable = decay_trend(
        np.random.default_rng(2).normal(0.0005, 0.01, 1000), folds(1000), periods_per_year=252
    )
    assert stable.gate_check(GATES).passed


def test_inputs_are_checked() -> None:
    with pytest.raises(ValueError, match="at least 4 folds"):
        decay_trend(np.zeros(30), folds(30, 3), periods_per_year=252)
    with pytest.raises(ValueError, match="missing"):
        decay_trend(np.r_[np.zeros(20), np.nan], folds(21), periods_per_year=252)
    with pytest.raises(ValueError, match="its fold"):
        decay_trend(np.zeros(20), folds(19), periods_per_year=252)
