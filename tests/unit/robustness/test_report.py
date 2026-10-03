"""ROB-008: the robustness report and score against config/gates.yaml — the simulated genuine
edge passes every R2 robustness gate and the simulated single-point optimum on noise fails; every
measure is reported with its gate; a gate that cannot be evaluated makes the verdict incomplete,
never a pass. A parameter-free strategy has every numeric constant perturbed, and its
neighbourhood gate is not applicable only when its parameters are declared fixed a priori with a
source (C-25, ADR 0058)."""

import dataclasses
import json

import numpy as np
import pytest

from helpers.quality import repo_config
from xq.risk.engine import RiskEngine
from xq.robustness.perturb import config_constants, with_constants
from xq.robustness.report import ROBUSTNESS_GATES, RobustnessReport, robustness_report
from xq.robustness.simulated import SimulationSpec, simulated_subject
from xq.robustness.subject import StrategySubject

CFG = repo_config()
GATES = CFG.gates_config()
FULL = CFG.validation_config()
#: Fewer Monte Carlo paths and noise draws than the report's defaults, for the test's speed.
SETTINGS = FULL.model_copy(
    update={
        "monte_carlo": FULL.monte_carlo.model_copy(update={"n_paths": 100}),
        "noise": FULL.noise.model_copy(update={"n_seeds": 3}),
    }
)
ENGINE = RiskEngine.from_config(CFG)


def report(truth: str, seed: int) -> RobustnessReport:
    subject = simulated_subject(
        SimulationSpec(truth=truth, seed=seed),  # type: ignore[arg-type]
        capital=CFG.backtest_config().capital_usd,
        periods_per_year=CFG.gate_periods_per_year(),
    )
    return robustness_report(subject, gates=GATES, settings=SETTINGS, risk_engine=ENGINE, seed=seed)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_a_genuine_edge_passes_every_robustness_gate(seed: int) -> None:
    result = report("genuine", seed)
    assert result.verdict == "pass", result.summary_lines()
    assert result.score == 1.0
    assert [c.criterion.key for c in result.checks] == list(ROBUSTNESS_GATES)
    assert result.not_evaluated == {}
    assert result.noise["price"].table()["retention"].min() > 0.8  # a trend ignores spread noise


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_a_single_point_optimum_on_noise_fails(seed: int) -> None:
    result = report("overfit", seed)
    assert result.verdict == "fail"
    assert result.score < 1.0
    hood = result.check("parameter_neighbourhood.profitable_share_min")
    assert hood is not None
    assert not hood.passed  # its neighbours are fresh noise
    assert result.noise == {}  # random positions read no market data ...
    assert set(result.noise_not_applicable) == {"price", "feature"}  # ... and the report says so


def test_every_measure_is_reported_and_serializable() -> None:
    result = report("genuine", 3)
    rows = result.results()
    ids = [(r.test_id, r.name) for r in rows]
    assert ids == [
        ("ROB-001", "parameter_perturbation"),
        ("ROB-002", "cost_stress"),
        ("ROB-003", "block_bootstrap"),
        ("ROB-003", "trade_permutation"),
        ("ROB-004", "monte_carlo"),
        ("ROB-005", "price_noise"),
        ("ROB-005", "feature_noise"),
        ("ROB-006", "slices"),
        ("ROB-007", "execution_delay"),
        ("WF", "positive_folds"),
        ("BT-003", "oos_max_drawdown"),
    ]
    gated = {r.name: r.passed for r in rows}
    assert gated["block_bootstrap"] is None  # reported, not gated
    assert gated["price_noise"] is None
    assert gated["monte_carlo"] is True
    json.dumps([dataclasses.asdict(r) for r in rows], allow_nan=False)  # JSON-safe
    text = result.markdown()
    assert text.startswith("## Robustness (ROB-001 ... ROB-008)")
    assert "SYNTHETIC DATA" in text
    for heading in ("ROB-001", "ROB-002", "ROB-003", "ROB-004", "ROB-005", "ROB-006", "ROB-007"):
        assert f"### {heading}" in text
    assert "One-at-a-time sensitivity" in text
    assert "robustness score 1.00 (7 of 7" in result.summary_lines()[0]


def test_a_gate_that_cannot_be_evaluated_makes_the_verdict_incomplete() -> None:
    subject = simulated_subject(
        SimulationSpec(truth="genuine", seed=0),
        capital=CFG.backtest_config().capital_usd,
        periods_per_year=CFG.gate_periods_per_year(),
    )
    fixed = dataclasses.replace(subject, parameters=(), evaluate=None)
    result = robustness_report(fixed, gates=GATES, settings=SETTINGS, risk_engine=ENGINE, seed=0)
    assert result.perturbation is None
    assert result.not_evaluated == {
        "parameter_neighbourhood.profitable_share_min": (
            "the strategy has no tunable parameters and no numeric constant to perturb"
        )
    }
    assert result.verdict == "incomplete"  # never a pass by default
    assert result.score == 1.0  # six of six evaluated gates passed
    assert any("not evaluated" in line for line in result.summary_lines())


HOOD = "parameter_neighbourhood.profitable_share_min"
SOURCE = "Moskowitz, Ooi and Pedersen (2012), 12-month time-series momentum"


def fixed_trend(**changes: object) -> StrategySubject:
    """The genuine trend edge as a parameter-free strategy: its lookback and deadband are
    constants of its configuration rather than a choice from a grid."""
    subject = simulated_subject(
        SimulationSpec(truth="genuine", seed=0),
        capital=CFG.backtest_config().capital_usd,
        periods_per_year=CFG.gate_periods_per_year(),
    )
    assert subject.evaluate is not None
    nominal = {p.name: p.nominal for p in subject.parameters}
    config = {
        "rule": "trend",
        "params": {"lookback": int(nominal["lookback"]), "deadband": nominal["deadband"]},
        "exit": 0.0,
        "long_only": False,
    }
    parameters, held = config_constants(config)
    evaluate = subject.evaluate

    def constants(values):  # type: ignore[no-untyped-def]
        changed = with_constants(config, values)["params"]
        return evaluate({"lookback": changed["lookback"], "deadband": changed["deadband"]})

    return dataclasses.replace(
        subject,
        parameters=parameters,
        evaluate=constants,
        parameter_kind="constants",
        held_constants=held,
        **changes,
    )


def test_a_parameter_free_strategy_has_every_numeric_constant_perturbed() -> None:
    subject = fixed_trend()
    assert [p.name for p in subject.parameters] == ["params.deadband", "params.lookback"]
    assert subject.held_constants == ("exit",)  # zero: no relative scale
    result = robustness_report(subject, gates=GATES, settings=SETTINGS, risk_engine=ENGINE, seed=0)
    hood = result.check(HOOD)
    assert hood is not None
    assert hood.passed  # the trend edge is a plateau in its constants
    assert result.not_applicable == {}
    assert result.verdict == "pass"
    (row,) = [r for r in result.results() if r.test_id == "ROB-001"]
    assert row.params["parameter_kind"] == "constants"
    assert row.params["held_constants"] == ["exit"]
    assert row.passed is True
    assert "every numeric constant of its configuration is perturbed" in result.markdown()


def test_parameters_fixed_a_priori_make_only_the_neighbourhood_not_applicable() -> None:
    subject = fixed_trend(parameters_fixed_a_priori=SOURCE)
    result = robustness_report(subject, gates=GATES, settings=SETTINGS, risk_engine=ENGINE, seed=0)
    assert result.not_applicable == {HOOD: f"parameters fixed a priori (source: {SOURCE})"}
    assert result.check(HOOD) is None  # not gated ...
    assert result.perturbation is not None  # ... but still reported
    (row,) = [r for r in result.results() if r.test_id == "ROB-001"]
    assert row.passed is None
    assert row.params["gated"] is False
    # every other gate still applies, and the verdict can pass without the neighbourhood
    assert [c.criterion.key for c in result.checks] == [k for k in ROBUSTNESS_GATES if k != HOOD]
    assert result.not_evaluated == {}
    assert result.verdict == "pass"
    assert result.score == 1.0
    assert f"R2 {HOOD}: not applicable: parameters fixed a priori" in "\n".join(
        result.summary_lines()
    )
    assert "Reported, not gated: parameters fixed a priori" in result.markdown()


def test_a_declaration_does_not_rescue_a_failing_gate() -> None:
    """The a-priori declaration removes one gate, never the others: a losing strategy still fails
    them."""
    subject = fixed_trend(parameters_fixed_a_priori=SOURCE)
    losing = dataclasses.replace(subject, returns=subject.returns - 0.002)
    result = robustness_report(losing, gates=GATES, settings=SETTINGS, risk_engine=ENGINE, seed=0)
    assert HOOD in result.not_applicable
    assert result.verdict == "fail"


def test_tuned_parameters_cannot_be_declared_fixed_a_priori() -> None:
    subject = simulated_subject(
        SimulationSpec(truth="genuine", seed=0),
        capital=CFG.backtest_config().capital_usd,
        periods_per_year=CFG.gate_periods_per_year(),
    )
    with pytest.raises(ValueError, match="not fixed a priori"):
        dataclasses.replace(subject, parameters_fixed_a_priori=SOURCE)
    with pytest.raises(ValueError, match="need a source"):
        dataclasses.replace(fixed_trend(), parameters_fixed_a_priori="  ")


def test_a_strategy_losing_at_its_nominal_point_is_reported_json_safe() -> None:
    """The median-to-nominal ratio is undefined (NaN) when the nominal Sharpe ratio is not
    positive; the robustness_results row stores it as null (a real board strategy found it)."""
    subject = fixed_trend()
    evaluate = subject.evaluate
    assert evaluate is not None
    losing = dataclasses.replace(subject, evaluate=lambda values: -np.asarray(evaluate(values)))
    result = robustness_report(losing, gates=GATES, settings=SETTINGS, risk_engine=ENGINE, seed=0)
    (row,) = [r for r in result.results() if r.test_id == "ROB-001"]
    assert row.metrics["median_to_nominal"] is None
    assert row.passed is False
    json.dumps([dataclasses.asdict(r) for r in result.results()], allow_nan=False)
