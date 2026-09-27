"""EXP-005: an experiment closes only with a complete conclusion, which is stored and appended to
the research log; the audit lists experiments still without one."""

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.pipeline import REPO, config
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.tracking import registry
from xq.tracking.conclusions import (
    ConclusionDoc,
    ConclusionError,
    Verdict,
    close_experiment,
    get_conclusion,
    load_conclusion,
    parse_conclusion,
    unconcluded_experiments,
)
from xq.tracking.db import create_db_engine, upgrade_to_head
from xq.tracking.registry import ExperimentStatus, RunStatus

LOG_HEADER = "# Research log\n"
FIELDS = {
    "observed": "Net Sharpe 0.1, 95 % CI [-0.4, 0.6].",
    "evidence": "Run 01RUN on ds-x;\nreport reports/x.",
    "interpretation": "No evidence of an edge.",
    "limitations": "Synthetic data only.",
    "action": "None.",
}


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    log = tmp_path / "docs" / "research" / "log.md"
    log.parent.mkdir(parents=True)
    log.write_text(LOG_HEADER)
    return config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    registry.add_hypothesis_version(
        engine, "H-0001", title="Baseline board", family_id="baselines", yaml_text="x\n"
    )
    yield engine
    engine.dispose()


def experiment_with_run(
    engine: Engine, *, confirmatory: bool, status: RunStatus | None = RunStatus.FINISHED
) -> str:
    experiment = registry.create_experiment(engine, "H-0001", "first board")
    if status is not None:
        run = registry.start_run(
            engine,
            experiment.experiment_id,
            kind="baseline_board",
            confirmatory=confirmatory,
            git_sha="abc",
            config_hash="h",
            config={},
            dataset_id="ds-x",
            lock_hash="l",
            seed=1,
            host="h",
        )
        if status is not RunStatus.RUNNING:
            registry.finish_run(engine, run.run_id, status)
    return experiment.experiment_id


def conclusion(verdict: str = "rejected", **changes: str) -> ConclusionDoc:
    return parse_conclusion({"verdict": verdict, **FIELDS, **changes})


def log_text(cfg: AppConfig) -> str:
    return cfg.paths.resolve(cfg.paths.research_log).read_text()


def test_closing_records_the_conclusion_and_appends_the_log(cfg: AppConfig, engine: Engine) -> None:
    experiment_id = experiment_with_run(engine, confirmatory=False)
    record = close_experiment(cfg, engine, experiment_id, conclusion("rejected"))
    assert record.verdict is Verdict.REJECTED
    assert get_conclusion(engine, experiment_id) == record
    experiment = registry.get_experiment(engine, experiment_id)
    assert experiment.status is ExperimentStatus.CLOSED
    assert experiment.verdict == "rejected"
    text = log_text(cfg)
    assert text.startswith(LOG_HEADER)
    assert "H-0001 v1: Baseline board — rejected" in text
    assert f"- **Experiment:** {experiment_id} (first board)" in text
    assert "- **Runs:** 1 (0 confirmatory)" in text
    assert "baseline_board, exploratory, finished, git abc, dataset ds-x" in text
    assert "- **Evidence:** Run 01RUN on ds-x;\n  report reports/x." in text
    for name in ("Observed", "Interpretation", "Limitations", "Action"):
        assert f"- **{name}:** " in text
    assert unconcluded_experiments(engine) == []


@pytest.mark.parametrize(
    "missing", ["observed", "evidence", "interpretation", "limitations", "action"]
)
def test_closing_without_a_complete_conclusion_fails(
    cfg: AppConfig, engine: Engine, missing: str
) -> None:
    experiment_with_run(engine, confirmatory=False)
    with pytest.raises(ConclusionError, match="non-empty"):
        conclusion(**{missing: "   "})
    data = {"verdict": "rejected", **FIELDS}
    del data[missing]
    with pytest.raises(ConclusionError, match="non-empty"):
        parse_conclusion(data)
    with pytest.raises(ConclusionError):
        parse_conclusion({**FIELDS, "verdict": "maybe"})
    assert len(unconcluded_experiments(engine)) == 1
    assert log_text(cfg) == LOG_HEADER


def test_supported_needs_a_finished_confirmatory_run(cfg: AppConfig, engine: Engine) -> None:
    exploratory = experiment_with_run(engine, confirmatory=False)
    with pytest.raises(ConclusionError, match="confirmatory run"):
        close_experiment(cfg, engine, exploratory, conclusion("supported"))
    assert registry.get_experiment(engine, exploratory).status is ExperimentStatus.OPEN
    failed = experiment_with_run(engine, confirmatory=True, status=RunStatus.FAILED)
    with pytest.raises(ConclusionError, match="confirmatory run"):
        close_experiment(cfg, engine, failed, conclusion("supported"))
    confirmed = experiment_with_run(engine, confirmatory=True)
    assert close_experiment(cfg, engine, confirmed, conclusion("supported")).verdict == "supported"


def test_running_runs_and_empty_experiments(cfg: AppConfig, engine: Engine) -> None:
    running = experiment_with_run(engine, confirmatory=True, status=RunStatus.RUNNING)
    with pytest.raises(ConclusionError, match="still running"):
        close_experiment(cfg, engine, running, conclusion("inconclusive"))
    empty = experiment_with_run(engine, confirmatory=False, status=None)
    with pytest.raises(ConclusionError, match="needs a finished run"):
        close_experiment(cfg, engine, empty, conclusion("rejected"))
    assert close_experiment(cfg, engine, empty, conclusion("inconclusive")).verdict == (
        "inconclusive"
    )


def test_a_conclusion_is_never_rewritten(cfg: AppConfig, engine: Engine) -> None:
    experiment_id = experiment_with_run(engine, confirmatory=False)
    close_experiment(cfg, engine, experiment_id, conclusion("rejected"))
    with pytest.raises(ConclusionError, match="already closed"):
        close_experiment(cfg, engine, experiment_id, conclusion("inconclusive"))
    with pytest.raises(ConclusionError, match="not found"):
        close_experiment(cfg, engine, "01NOTANEXPERIMENT000000000", conclusion())
    # A new run of the hypothesis opens a new experiment.
    fresh = registry.open_experiment_for(engine, "H-0001", "second board")
    assert fresh.experiment_id != experiment_id


def test_a_missing_log_leaves_the_experiment_open(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    experiment_id = experiment_with_run(engine, confirmatory=False)
    cfg.paths.resolve(cfg.paths.research_log).unlink()
    with pytest.raises(ConclusionError, match="research log"):
        close_experiment(cfg, engine, experiment_id, conclusion())
    assert registry.get_experiment(engine, experiment_id).status is ExperimentStatus.OPEN
    with pytest.raises(ConclusionError, match="no conclusion"):
        get_conclusion(engine, experiment_id)


def test_the_template_is_a_valid_conclusion() -> None:
    template = load_conclusion(REPO / "experiments" / "conclusions" / "TEMPLATE.yaml")
    assert template.verdict is Verdict.INCONCLUSIVE
    with pytest.raises(ConclusionError, match="not found"):
        load_conclusion(REPO / "experiments" / "conclusions" / "missing.yaml")


def test_repository_research_log_exists() -> None:
    assert (REPO / "docs" / "research" / "log.md").read_text().startswith(LOG_HEADER)


def invoke(root: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        app,
        [
            "--config-dir",
            str(REPO / "config"),
            "--set",
            f"paths.root={root}",
            "--set",
            f"paths.migrations_dir={REPO / 'migrations'}",
            "--set",
            "logging.console=false",
            "--set",
            "logging.file=null",
            *args,
        ],
    )
    return result.exit_code, result.output


def test_cli_audit_and_close(cfg: AppConfig, engine: Engine, tmp_path: Path) -> None:
    experiment_id = experiment_with_run(engine, confirmatory=False)
    code, output = invoke(tmp_path, "exp", "audit")
    assert code == 1
    assert experiment_id in output
    assert "1 experiment(s) without a conclusion" in output
    path = tmp_path / "conclusion.yaml"
    path.write_text(yaml.safe_dump({"verdict": "rejected", **FIELDS}))
    code, output = invoke(tmp_path, "exp", "close", experiment_id, "--conclusion", str(path))
    assert code == 0, output
    assert f"experiment {experiment_id} closed: rejected" in output
    code, output = invoke(tmp_path, "exp", "audit")
    assert code == 0
    assert "0 experiment(s) without a conclusion" in output
    path.write_text(yaml.safe_dump({"verdict": "rejected", **FIELDS, "action": ""}))
    other = experiment_with_run(engine, confirmatory=False)
    code, output = invoke(tmp_path, "exp", "close", other, "--conclusion", str(path))
    assert code == 2
    assert "non-empty" in output
