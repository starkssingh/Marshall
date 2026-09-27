"""ROB-001: parameter perturbation and plateau metrics — a single-point optimum on noise fails
the R2 neighbourhood gate, a genuine trend edge chosen the same way passes, and the designs
(one at a time, jointly, heat maps) evaluate the points they state."""

from collections.abc import Mapping

import numpy as np
import pytest

from helpers.quality import repo_config
from helpers.strategies import (
    drift_returns,
    overfit_grid,
    overfit_returns,
    sharpe,
    strategy_returns,
    trend_positions,
)
from xq.robustness.perturb import Parameter, perturb

GATES = repo_config().gates_config()
HOOD = GATES.r2_validated.parameter_neighbourhood
A_GRID = [float(a) for a in range(5, 15)]
B_GRID = [1.0, 2.0, 3.0, 4.0, 5.0]


def overfit_case(salt: int) -> tuple[float, float, float, bool]:
    """Pick the in-sample best point of the overfit grid, then perturb it."""
    matrix, points = overfit_grid(A_GRID, B_GRID, 1000, salt=salt)
    a, b = points[int(np.argmax([sharpe(matrix[:, k]) for k in range(matrix.shape[1])]))]
    result = perturb(
        lambda v: overfit_returns(v["a"], v["b"], 1000, salt=salt),
        [Parameter("a", a), Parameter("b", b)],
        periods_per_year=252,
        heatmaps=False,
    )
    check = result.gate_check(GATES)
    return (
        result.nominal_sharpe,
        result.median_to_nominal(HOOD.perturbation),
        check.value,
        check.passed,
    )


def genuine_case(seed: int) -> tuple[float, float, float, bool]:
    """The trend rule on drifting returns, its parameters picked in sample the same way."""
    returns = drift_returns(2500, seed=seed, drift_sigma=0.0004)

    def evaluate(values: Mapping[str, float]) -> np.ndarray:
        positions = trend_positions(returns, int(values["lookback"]), values["deadband"])
        return strategy_returns(returns, positions, cost=0.0002)

    grid = [(k, d) for k in (20, 30, 40, 50, 60, 80) for d in (0.25, 0.5, 1.0)]
    lookback, deadband = max(
        grid, key=lambda p: sharpe(evaluate({"lookback": p[0], "deadband": p[1]}))
    )
    result = perturb(
        evaluate,
        [Parameter("lookback", lookback, integer=True, minimum=2), Parameter("deadband", deadband)],
        periods_per_year=252,
        heatmaps=False,
    )
    check = result.gate_check(GATES)
    return (
        result.nominal_sharpe,
        result.median_to_nominal(HOOD.perturbation),
        check.value,
        check.passed,
    )


def test_a_single_point_optimum_on_noise_fails_the_neighbourhood_gate() -> None:
    cases = np.array([overfit_case(salt) for salt in range(100)])
    nominal, ratio, share, passed = cases.T
    assert np.all(nominal > 0.5)  # every winner looks good in sample ...
    # ... but its 8 neighbours are fresh noise: P(>= 6 of 8 positive) = 37/256 = 14.5 %
    assert passed.mean() <= 0.25
    assert np.nanmean(ratio) < 0.25  # the neighbourhood's median is near zero, not near the peak
    assert share.mean() == pytest.approx(0.5, abs=0.1)


def test_a_genuine_trend_edge_passes_the_neighbourhood_gate() -> None:
    cases = np.array([genuine_case(seed) for seed in range(60)])
    _, ratio, share, passed = cases.T
    assert passed.mean() >= 0.9
    assert np.median(ratio) > 0.7  # a plateau: the neighbours keep most of the nominal Sharpe
    assert np.median(share) == 1.0


def test_the_designs_evaluate_the_points_they_state() -> None:
    calls: list[dict[str, float]] = []

    def evaluate(values: Mapping[str, float]) -> np.ndarray:
        calls.append(dict(values))
        # a smooth bowl peaking at (40, 0.5): Sharpe falls with the distance from the peak
        edge = (
            0.002 - 1e-6 * (values["lookback"] - 40) ** 2 - 0.004 * (values["deadband"] - 0.5) ** 2
        )
        return edge + np.random.default_rng(0).normal(0.0, 0.01, 500)

    result = perturb(
        evaluate,
        [Parameter("lookback", 40, integer=True, minimum=2), Parameter("deadband", 0.5)],
        periods_per_year=252,
    )
    assert len(calls) == len(result.points) == len({tuple(c.items()) for c in calls})  # once each
    assert all(isinstance(c["lookback"], int) for c in calls)
    single = result.one_at_a_time()
    assert len(single) == 3 * 2 * 2  # three levels, two parameters, down and up
    lookbacks = single.loc[single["parameter"] == "lookback", "value"].tolist()
    assert lookbacks == [36.0, 44.0, 32.0, 48.0, 28.0, 52.0]
    hood = result.neighbourhood(0.2)
    assert len(hood) == 3**2 - 1
    assert set(hood["lookback"]) == {32.0, 40.0, 48.0}
    assert set(np.round(hood["deadband"], 12)) == {0.4, 0.5, 0.6}
    heat = result.heatmap("lookback", "deadband")
    assert heat.shape == (7, 7)
    assert heat.loc[0.5, 40.0] == pytest.approx(result.nominal_sharpe)
    assert heat.loc[0.5, 40.0] == heat.to_numpy().max()  # the bowl's peak
    summary = result.summary()
    assert list(summary.index) == [0.1, 0.2, 0.3]
    assert (summary["profitable_share"] == 1.0).all()
    assert summary["median_to_nominal"].is_monotonic_decreasing  # wider rings, lower medians
    check = result.gate_check(GATES)
    assert check.passed
    assert check.criterion.key == "parameter_neighbourhood.profitable_share_min"
    assert check.criterion.threshold == HOOD.profitable_share_min
    assert "pass" in check.describe()


def test_integer_choice_and_scaled_parameters_move_to_valid_values() -> None:
    assert Parameter("k", 3, integer=True).neighbours(0.1) == (2.0, 4.0)  # at least one step
    assert Parameter("k", 10, integer=True).neighbours(0.25) == (8.0, 13.0)  # 7.5 -> 8, 12.5 -> 13
    assert Parameter("k", 2, integer=True, minimum=2).neighbours(0.3) == (3.0,)
    grid = Parameter("tf", 15, choices=(1, 5, 15, 30, 60))
    assert grid.neighbours(0.2) == (5.0, 30.0)  # the neighbouring discrete values
    assert Parameter("tf", 60, choices=(1, 5, 15, 30, 60)).neighbours(0.2) == (30.0,)
    assert Parameter("theta", 0.0, scale=0.5).neighbours(0.2) == (-0.1, 0.1)
    with pytest.raises(ValueError, match="scale"):
        Parameter("theta", 0.0)
    with pytest.raises(ValueError, match="choices"):
        Parameter("tf", 20, choices=(15, 30))
    with pytest.raises(ValueError, match="integer nominal"):
        Parameter("k", 2.5, integer=True)


def test_points_without_variance_are_not_profitable_and_inputs_are_checked() -> None:
    def evaluate(values: Mapping[str, float]) -> np.ndarray:
        if values["x"] != 1.0:
            return np.zeros(100)  # never trades
        return np.random.default_rng(1).normal(0.001, 0.01, 100)

    result = perturb(evaluate, [Parameter("x", 1.0)], periods_per_year=252)
    assert result.profitable_share(0.2) == 0.0
    assert not result.gate_check(GATES).passed
    with pytest.raises(ValueError, match=r"\(0, 1\)"):
        perturb(evaluate, [Parameter("x", 1.0)], periods_per_year=252, levels=[1.5])
    with pytest.raises(ValueError, match="distinct"):
        perturb(evaluate, [Parameter("x", 1.0), Parameter("x", 2.0)], periods_per_year=252)
    with pytest.raises(ValueError, match="not evaluated"):
        result.neighbourhood(0.25)
