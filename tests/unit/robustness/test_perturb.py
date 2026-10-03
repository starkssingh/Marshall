"""ROB-001: parameter perturbation and plateau metrics — a single-point optimum on noise fails
the R2 neighbourhood gate, a genuine trend edge chosen the same way passes, a ridge (good only
along the diagonal) fails the full-grid gate, the joint grid is sampled deterministically above
243 points, and the designs (one at a time with its sensitivity table, jointly, heat maps)
evaluate the points they state (C-24, ADR 0055)."""

from collections.abc import Callable, Mapping

import numpy as np
import pandas as pd
import pytest

from helpers.quality import repo_config
from helpers.strategies import (
    drift_returns,
    overfit_grid,
    overfit_returns,
    point_seed,
    sharpe,
    strategy_returns,
    trend_positions,
)
from xq.robustness.perturb import Parameter, config_constants, perturb, with_constants

CFG = repo_config()
GATES = CFG.gates_config()
HOOD = GATES.r2_validated.parameter_neighbourhood
MAX_POINTS = CFG.validation_config().perturbation.max_joint_points
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
        max_points=MAX_POINTS,
        seed=0,
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
        max_points=MAX_POINTS,
        seed=0,
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
        max_points=MAX_POINTS,
        seed=0,
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

    result = perturb(
        evaluate, [Parameter("x", 1.0)], periods_per_year=252, max_points=MAX_POINTS, seed=0
    )
    assert result.profitable_share(0.2) == 0.0
    assert not result.gate_check(GATES).passed
    with pytest.raises(ValueError, match=r"\(0, 1\)"):
        perturb(
            evaluate,
            [Parameter("x", 1.0)],
            periods_per_year=252,
            max_points=9,
            seed=0,
            levels=[1.5],
        )
    with pytest.raises(ValueError, match="distinct"):
        perturb(
            evaluate,
            [Parameter("x", 1.0), Parameter("x", 2.0)],
            periods_per_year=252,
            max_points=9,
            seed=0,
        )
    with pytest.raises(ValueError, match="not evaluated"):
        result.neighbourhood(0.25)


def ridge_evaluate(
    names: tuple[str, ...], periods: int = 2000
) -> Callable[[Mapping[str, float]], np.ndarray]:
    """A ridge-shaped optimum: profitable only where every parameter moved by the same relative
    step as the others (the diagonal through the nominal point), losing elsewhere."""

    def evaluate(values: Mapping[str, float]) -> np.ndarray:
        logs = [np.log(values[name] / 10.0) for name in names]
        on_ridge = max(logs) - min(logs) < 1e-9
        edge = 0.001 if on_ridge else -0.001
        noise = np.random.default_rng(point_seed(*(values[n] for n in names)))
        return edge + noise.normal(0.0, 0.01, periods)

    return evaluate


@pytest.mark.parametrize("k", [2, 3])
def test_a_ridge_optimum_fails_the_full_grid_gate(k: int) -> None:
    # C-24 (3): good only along the diagonal. Of the 3^k - 1 neighbours, two lie on the ridge
    # (every parameter down, every parameter up), so the profitable share is 2 / (3^k - 1).
    names = tuple(f"p{i}" for i in range(k))
    result = perturb(
        ridge_evaluate(names),
        [Parameter(name, 10.0) for name in names],
        periods_per_year=252,
        max_points=MAX_POINTS,
        seed=0,
        heatmaps=False,
    )
    assert result.nominal_sharpe > 1.0  # the chosen point looks good ...
    hood = result.neighbourhood(HOOD.perturbation)
    assert len(hood) == 3**k - 1
    assert result.profitable_share(HOOD.perturbation) == pytest.approx(2 / (3**k - 1))
    check = result.gate_check(GATES)
    assert not check.passed  # ... but the plateau is a knife edge
    # the one-at-a-time table shows every single move losing: the report names the fragility
    table = result.sensitivity()
    assert list(table.index) == list(names)
    assert (table["profitable_share"] == 0.0).all()
    assert (table["worst_change"] < -1.0).all()
    assert list(table.columns[1:7]) == ["-10%", "+10%", "-20%", "+20%", "-30%", "+30%"]


def test_a_large_grid_is_sampled_deterministically() -> None:
    # six parameters: 3^6 - 1 = 728 neighbours, more than 243, so 243 are drawn from the seed
    names = tuple(f"p{i}" for i in range(6))
    calls: list[tuple[float, ...]] = []

    def evaluate(values: Mapping[str, float]) -> np.ndarray:
        calls.append(tuple(values.get(n, 10.0) for n in names))
        return 0.0005 + np.random.default_rng(len(calls)).normal(0.0, 0.01, 200)

    params = [Parameter(name, 10.0) for name in names]

    def run(seed: int) -> pd.DataFrame:
        result = perturb(
            evaluate,
            params,
            periods_per_year=252,
            max_points=MAX_POINTS,
            seed=seed,
            levels=[HOOD.perturbation],
            heatmaps=False,
        )
        assert result.neighbourhood_design(HOOD.perturbation) == (
            "seeded sample of 243 of the grid's 728 points around the nominal"
        )
        return result.neighbourhood(HOOD.perturbation)

    first = run(1)
    assert len(first) == MAX_POINTS
    grid = first[list(names)].to_numpy()
    assert set(np.round(grid.ravel(), 9)) <= {8.0, 10.0, 12.0}  # every point is on the grid
    assert len({tuple(row) for row in grid}) == MAX_POINTS  # drawn without replacement
    assert not any(np.all(grid == 10.0, axis=1))  # the nominal point is not a neighbour
    np.testing.assert_array_equal(run(1)[list(names)].to_numpy(), grid)  # the same seed ...
    assert not np.array_equal(run(2)[list(names)].to_numpy(), grid)  # ... and another one
    five = perturb(
        evaluate,
        params[:5],
        periods_per_year=252,
        max_points=MAX_POINTS,
        seed=1,
        levels=[HOOD.perturbation],
        heatmaps=False,
    )
    assert len(five.neighbourhood(HOOD.perturbation)) == 3**5 - 1  # five parameters: in full
    assert five.neighbourhood_design(HOOD.perturbation).startswith("full grid: 242 points")


def test_every_numeric_constant_of_a_configuration_is_a_parameter() -> None:
    """C-25 (4): a parameter-free strategy's numeric constants, by dotted path; zeros are held,
    booleans and text are not numbers."""
    config = {
        "rule": "ma_crossover",
        "params": {"fast": 20, "slow": 50, "exit": 0.0},
        "vol_target": {"annual_vol": 0.1, "lookback": 20, "enabled": True},
        "offset": -3,
    }
    parameters, held = config_constants(config)
    by_name = {p.name: p for p in parameters}
    assert list(by_name) == [
        "offset",
        "params.fast",
        "params.slow",
        "vol_target.annual_vol",
        "vol_target.lookback",
    ]
    assert held == ("params.exit",)
    assert by_name["params.fast"].integer
    assert by_name["params.fast"].minimum == 1.0
    assert by_name["offset"].integer
    assert by_name["offset"].minimum is None
    assert not by_name["vol_target.annual_vol"].integer
    changed = with_constants(config, {"params.fast": 24.0, "vol_target.annual_vol": 0.12})
    assert changed["params"] == {"fast": 24, "slow": 50, "exit": 0.0}
    assert isinstance(changed["params"]["fast"], int)
    assert changed["vol_target"]["annual_vol"] == 0.12
    assert config["params"]["fast"] == 20  # the original is left as it was
    with pytest.raises(KeyError, match="not a constant"):
        with_constants(config, {"params.missing": 1.0})
