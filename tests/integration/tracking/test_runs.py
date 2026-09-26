"""EXP-003: the run context captures code, config, data and environment; dirty trees are refused
for confirmatory runs."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from helpers.pipeline import REPO, config
from xq.core.config import AppConfig
from xq.core.errors import XQError
from xq.data.raw_store import sha256_file
from xq.datasets.builder import NoDatasetDataError
from xq.tracking import registry
from xq.tracking.db import create_db_engine, upgrade_to_head
from xq.tracking.runs import RunContextError, experiment_run, run_config_hash


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "uv.lock").write_text("version = 1\n")
    (root / ".gitignore").write_text("data/\nlogs/\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def cfg(repo: Path) -> AppConfig:
    return config(repo)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    registry.add_hypothesis_version(
        engine, "H-0001", title="t", family_id="momentum", yaml_text="statement: x\n"
    )
    yield engine
    engine.dispose()


def test_confirmatory_run_records_its_provenance(
    cfg: AppConfig, engine: Engine, repo: Path
) -> None:
    config_values = {"lookback": 20, "costs": "baseline"}
    with experiment_run(cfg, engine, "H-0001", config_values, kind="baseline", seed=11) as run:
        assert run.confirmatory
        run.log_metric("sharpe_net", 0.3, fold_id="f1")
        report = repo / "data" / "report.md"  # git-ignored: logging it keeps the tree clean
        report.write_text("ok\n")
        run.log_artifact(report, kind="report")
        draw = run.rng.normal()
    stored = registry.get_run(engine, run.run_id)
    assert stored.status is registry.RunStatus.FINISHED
    assert stored.finished_at is not None
    assert stored.git_sha == git(repo, "rev-parse", "HEAD")
    assert stored.lock_hash == sha256_file(repo / "uv.lock")
    assert stored.seed == 11
    assert stored.host
    assert stored.config["run"] == config_values
    assert stored.config_hash == run_config_hash(cfg, config_values)
    assert stored.dataset_id is None
    assert [m.name for m in registry.get_metrics(engine, run.run_id)] == ["sharpe_net"]
    assert registry.list_artifacts(engine, run.run_id)[0].kind == "report"
    # Same seed, same random stream.
    with experiment_run(cfg, engine, "H-0001", config_values, kind="baseline", seed=11) as again:
        assert again.rng.normal() == draw


def test_dirty_tree_is_refused_for_confirmatory_runs(
    cfg: AppConfig, engine: Engine, repo: Path
) -> None:
    (repo / "uv.lock").write_text("version = 2\n")
    with (
        pytest.raises(RunContextError, match="uncommitted changes"),
        experiment_run(cfg, engine, "H-0001", {}, kind="baseline", seed=1),
    ):
        pass
    assert registry.count_runs(engine) == 0
    with experiment_run(
        cfg, engine, "H-0001", {}, kind="baseline", seed=1, exploratory=True
    ) as run:
        assert not run.confirmatory
    stored = registry.get_run(engine, run.run_id)
    assert stored.git_sha.endswith("+dirty")
    assert not stored.confirmatory


def test_unidentifiable_code_or_missing_lockfile_is_refused(tmp_path: Path) -> None:
    cfg = config(tmp_path)  # not a git repository, no uv.lock
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    registry.add_hypothesis_version(engine, "H-0001", title="t", family_id="f", yaml_text="x\n")
    with (
        pytest.raises(RunContextError, match=r"cannot be determined.*uv.lock is missing"),
        experiment_run(cfg, engine, "H-0001", {}, kind="baseline", seed=1),
    ):
        pass
    with experiment_run(
        cfg, engine, "H-0001", {}, kind="baseline", seed=1, exploratory=True
    ) as run:
        pass
    assert registry.get_run(engine, run.run_id).lock_hash == "missing"
    engine.dispose()


def test_a_failing_block_marks_the_run_failed(cfg: AppConfig, engine: Engine) -> None:
    with (
        pytest.raises(ZeroDivisionError),
        experiment_run(cfg, engine, "H-0001", {}, kind="baseline", seed=1) as run,
    ):
        1 / 0  # noqa: B018
    assert registry.get_run(engine, run.run_id).status is registry.RunStatus.FAILED


def test_inputs_are_checked_before_anything_is_recorded(cfg: AppConfig, engine: Engine) -> None:
    with (
        pytest.raises(XQError, match="not registered"),
        experiment_run(cfg, engine, "H-0404", {}, kind="baseline", seed=1),
    ):
        pass
    with (
        pytest.raises(NoDatasetDataError, match="not found"),
        experiment_run(
            cfg, engine, "H-0001", {}, kind="baseline", seed=1, dataset_id="ds-0000000000000000"
        ),
    ):
        pass
    assert registry.count_runs(engine) == 0


def test_runs_share_the_open_experiment(cfg: AppConfig, engine: Engine) -> None:
    with experiment_run(cfg, engine, "H-0001", {"a": 1}, kind="baseline", seed=1) as first:
        pass
    with experiment_run(cfg, engine, "H-0001", {"a": 2}, kind="baseline", seed=2) as second:
        pass
    assert first.run.experiment_id == second.run.experiment_id
    assert first.run.config_hash != second.run.config_hash
