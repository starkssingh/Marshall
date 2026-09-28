"""`xq validate-strategy <run_id>` end to end on the known-truth simulated strategies (synthetic
data only, ADR 0056): a recorded genuine trend edge passes R1 and R2, a recorded single-point
optimum on noise fails R2. The report, its stat_tests and robustness_results rows, and the
hypothesis's slices are produced; a validation adds no trials; runs that cannot be validated are
refused."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.pipeline import REPO, config
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.tracking import registry
from xq.tracking.db import create_db_engine
from xq.tracking.trials import trial_count
from xq.validation.strategy import SIMULATED_KIND, VALIDATION_KIND

TEMPLATE = REPO / "experiments" / "hypotheses" / "TEMPLATE.yaml"
#: Fewer Monte Carlo paths, noise draws and size-check families than the defaults, for speed.
FAST = (
    "validation.monte_carlo.n_paths=100",
    "validation.noise.n_seeds=3",
    "validation.spa_size_check.n_sim=100",
)


def xq(root: Path, *args: str) -> tuple[int, str]:
    common = [
        "--config-dir",
        str(REPO / "config"),
        "--profile",
        "research",
        "--set",
        f"paths.root={root}",
        "--set",
        f"paths.migrations_dir={REPO / 'migrations'}",
        "--set",
        "logging.console=false",
        "--set",
        "logging.file=null",
    ]
    for item in FAST:
        common += ["--set", item]
    result = CliRunner().invoke(app, [*common, *args])
    return result.exit_code, result.output


def hypothesis(root: Path, hypothesis_id: str, family: str, **extra: Any) -> None:
    data: dict[str, Any] = yaml.safe_load(TEMPLATE.read_text())
    data.update(
        {
            "id": hypothesis_id,
            "family": family,
            "title": f"simulated strategy ({family})",
            "slices": ["year", "volatility tercile"],
            **extra,
        }
    )
    path = root / f"{hypothesis_id}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    code, output = xq(root, "exp", "register", str(path))
    assert code == 0, output


def simulate(root: Path, truth: str, seed: int, hypothesis_id: str) -> str:
    code, output = xq(
        root, "robustness", "simulate", "--truth", truth, "--seed", str(seed),
        "--hypothesis", hypothesis_id,
    )  # fmt: skip
    assert code == 0, output
    assert "(synthetic)" in output
    return output.split()[1].rstrip(":")


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("validate")
    hypothesis(root, "H-0900", "simulated_trend")
    hypothesis(root, "H-0901", "simulated_noise")
    return root


@pytest.fixture(scope="module")
def cfg(root: Path) -> AppConfig:
    return config(root)


@pytest.fixture(scope="module")
def engine(cfg: AppConfig) -> Engine:
    return create_db_engine(cfg.database_url())


def validation_of(engine: Engine, run_id: str) -> registry.RunRef:
    (run,) = [
        r
        for r in registry.list_runs(engine)
        if r.kind == VALIDATION_KIND and r.config["run"]["validates"] == run_id
    ]
    return run


def test_a_recorded_genuine_edge_passes_r1_and_r2(
    root: Path, cfg: AppConfig, engine: Engine
) -> None:
    run_id = simulate(root, "genuine", 0, "H-0900")
    simulated = registry.get_run(engine, run_id)
    assert simulated.kind == SIMULATED_KIND
    assert not simulated.confirmatory  # synthetic: never confirmatory
    before = trial_count(cfg, engine, "simulated_trend")
    assert before.n_trials == 24  # every configuration of the family is a trial

    code, output = xq(root, "validate-strategy", run_id, "--exploratory")
    assert code == 0, output
    lines = output.splitlines()
    assert lines[0].startswith("SYNTHETIC")
    assert "R1: PASS" in lines
    assert "R2: PASS" in lines
    assert "robustness score 1.00 (7 of 7" in output
    assert "WARNING" not in output  # an iid-like daily family: SPA's size check does not warn

    after = trial_count(cfg, engine, "simulated_trend")
    assert after.n_trials == before.n_trials  # a validation selects nothing: no trials

    validation = validation_of(engine, run_id)
    report_dir = (
        cfg.paths.resolve(cfg.paths.reports_dir) / "validation" / run_id / validation.run_id
    )
    markdown = (report_dir / "report.md").read_text()
    assert markdown.startswith("# Strategy validation: lookback=")
    assert "SYNTHETIC DATA" in markdown
    assert "**volatility_tercile** (descriptive, cut ex post)" in markdown  # declared slices
    payload = json.loads((report_dir / "report.json").read_text())
    assert payload["verdicts"] == {"R1": "pass", "R2": "pass"}
    assert payload["synthetic"] is True
    kinds = {a.kind for a in registry.list_artifacts(engine, validation.run_id)}
    assert kinds == {"validation_report", "validation_json"}

    tests = {t["test_name"]: t for t in registry.list_stat_tests(engine, validation.run_id)}
    assert {"sharpe_bootstrap", "deflated_sharpe", "pbo", "spa", "reality_check"} <= set(tests)
    assert tests["spa"]["p_value"] <= 0.10
    assert tests["deflated_sharpe"]["params"]["gated"] == "effective"
    configurations = [t for name, t in tests.items() if name.startswith("configuration:")]
    assert len(configurations) == 24
    assert all(t["adjusted_p"] >= t["p_value"] for t in configurations)  # Holm only raises p
    results = registry.list_robustness_results(engine, validation.run_id)
    assert [r["test_id"] for r in results][:2] == ["ROB-001", "ROB-002"]
    assert {r["name"]: r["passed"] for r in results}["monte_carlo"] is True
    metrics = {m.name: m.value for m in registry.get_metrics(engine, validation.run_id)}
    assert metrics["validation/R2_pass"] == 1.0


def test_a_recorded_single_point_optimum_on_noise_fails_r2(root: Path, engine: Engine) -> None:
    run_id = simulate(root, "overfit", 0, "H-0901")
    code, output = xq(root, "validate-strategy", run_id, "--exploratory")
    assert code == 0, output  # a failed validation is a result, not an error
    assert "R2: FAIL" in output.splitlines()
    for key in ("dsr_min", "pbo_max", "spa_p_max", "parameter_neighbourhood.profitable_share_min"):
        line = next(x for x in output.splitlines() if f"R2 {key}:" in x)
        assert ") FAIL" in line, line  # a warning may follow the outcome
    metrics = {
        m.name: m.value for m in registry.get_metrics(engine, validation_of(engine, run_id).run_id)
    }
    assert metrics["validation/R2_pass"] == 0.0


def test_runs_that_cannot_be_validated_are_refused(root: Path, engine: Engine) -> None:
    validation = next(r for r in registry.list_runs(engine) if r.kind == VALIDATION_KIND)
    code, output = xq(root, "validate-strategy", validation.run_id, "--exploratory")
    assert code == 2
    assert "no subject adapter" in output
    simulated = next(r for r in registry.list_runs(engine) if r.kind == SIMULATED_KIND)
    code, output = xq(
        root, "validate-strategy", simulated.run_id, "--strategy", "lookback=2,deadband=9",
        "--exploratory",
    )  # fmt: skip
    assert code == 2
    assert "not 'lookback=2,deadband=9'" in output
    code, output = xq(root, "robustness", "simulate", "--truth", "lucky", "--hypothesis", "H-0900")
    assert code == 2
    assert "invalid simulation" in output


def test_a_strategy_tuned_on_a_grid_cannot_claim_parameters_fixed_a_priori(root: Path) -> None:
    """C-25 (4): the declaration would be false for a candidate chosen from a grid, so the
    neighbourhood gate cannot be switched off for it."""
    hypothesis(
        root,
        "H-0903",
        "simulated_claims_fixed",
        parameters_fixed_a_priori=True,
        source="a claim the run contradicts",
    )
    run_id = simulate(root, "genuine", 5, "H-0903")
    code, output = xq(root, "validate-strategy", run_id, "--exploratory")
    assert code == 2
    assert "declares its parameters fixed a priori" in output
    assert "grid of 24 configurations" in output
