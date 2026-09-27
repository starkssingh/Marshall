"""ROB-008: the robustness report and score against config/gates.yaml — the simulated genuine
edge passes every R2 robustness gate and the simulated single-point optimum on noise fails; every
measure is reported with its gate; a gate that cannot be evaluated makes the verdict incomplete,
never a pass."""

import dataclasses
import json

import pytest

from helpers.quality import repo_config
from xq.risk.engine import RiskEngine
from xq.robustness.report import ROBUSTNESS_GATES, RobustnessReport, robustness_report
from xq.robustness.simulated import SimulationSpec, simulated_subject

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
        "parameter_neighbourhood.profitable_share_min": "the strategy has no tunable parameters"
    }
    assert result.verdict == "incomplete"  # never a pass by default
    assert result.score == 1.0  # six of six evaluated gates passed
    assert any("not evaluated" in line for line in result.summary_lines())
