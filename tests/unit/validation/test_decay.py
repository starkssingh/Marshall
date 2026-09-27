"""R2 decay trend: the Newey-West test of a negative slope of performance over time keeps its
size on stable edges (iid and autocorrelated), detects an edge fading away, and feeds the
decay_trend gate."""

import numpy as np
import pytest

from helpers.quality import repo_config
from helpers.simulate import ar1
from xq.validation.decay import decay_trend

GATES = repo_config().gates_config()


def rejection_rate(make, replications: int) -> float:  # type: ignore[no-untyped-def]
    return float(
        np.mean(
            [decay_trend(make(s), periods_per_year=252).p_value < 0.05 for s in range(replications)]
        )
    )


def test_a_stable_edge_is_rejected_at_about_the_nominal_rate() -> None:
    iid = rejection_rate(lambda s: np.random.default_rng(s).normal(0.0005, 0.01, 1000), 400)
    assert 0.02 <= iid <= 0.08  # 400 replications: the standard error at 5 % is 1.1 points
    dependent = rejection_rate(lambda s: 0.0005 + 0.01 * ar1(1000, 0.3, seed=s), 400)
    assert 0.02 <= dependent <= 0.10  # the HAC variance keeps autocorrelation from inflating it


def test_a_fading_edge_is_detected_and_fails_the_gate() -> None:
    drift = np.linspace(0.002, -0.001, 1000)  # 20 bp a day falling to -10 bp over four years
    rate = rejection_rate(lambda s: drift + np.random.default_rng(s).normal(0, 0.01, 1000), 100)
    assert rate > 0.8
    trend = decay_trend(
        drift + np.random.default_rng(1).normal(0, 0.01, 1000), periods_per_year=252
    )
    assert trend.slope_per_year == pytest.approx(-0.003 / (999 / 252), rel=0.5)
    check = trend.gate_check(GATES)
    assert check.criterion.key == "decay_trend.significance"
    assert not check.passed
    stable = decay_trend(np.random.default_rng(2).normal(0.0005, 0.01, 1000), periods_per_year=252)
    assert stable.gate_check(GATES).passed


def test_inputs_are_checked() -> None:
    with pytest.raises(ValueError, match="ten returns"):
        decay_trend(np.zeros(5), periods_per_year=252)
    with pytest.raises(ValueError, match="missing"):
        decay_trend(np.r_[np.zeros(20), np.nan], periods_per_year=252)
