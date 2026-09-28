"""EXP-006: `xq exp reproduce <run_id>` rebuilds a fixture board run's dataset, repeats the run and
matches every judged metric within tolerance without counting its trials again; a result that
changed, altered dataset content and runs without a reproducer are refused; a deleted dataset is
rebuilt from the spec the registry recorded. C-24 (5): the status is REPRODUCED only with the same
git sha, config hash and lock hash; a rerun on another commit or configuration is
RERUN_DIFFERENT_CODE and never counts as reproduced."""

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine, select, update
from typer.testing import CliRunner

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import REPO, config
from helpers.ticks import dense_ticks, write_mt5
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.datasets.builder import DatasetRef, build_dataset, datasets_root
from xq.tracking import registry
from xq.tracking.db import session_factory
from xq.tracking.models import Metric
from xq.tracking.registry import RunStatus
from xq.tracking.reproduce import REPRODUCTION_KIND, ReproductionError, reproduce_run
from xq.tracking.trials import trial_count

BOARD = {
    "targets": ["fwd_ret_mid_1h"],
    "walk_forward": {"min_train": "5D", "test_len": "2D", "embargo": "1h"},
    "forecast_baselines": ["zero_return", "historical_mean", "ar1"],
    "signal_timeframe": "1h",
    "rules": {
        "buy_and_hold": {"rule": "buy_and_hold"},
        "tsmom_8": {"rule": "time_series_momentum", "params": {"lookback": 8}},
    },
    "vol_target": {"annual_vol": 0.10, "lookback": 12, "max_exposure": 2.0},
    "random_entry_seeds": 4,
}


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


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clean git repository: what runs write (data, logs, reports) is ignored, so the tree
    stays clean and every run records the commit's sha."""
    root = tmp_path_factory.mktemp("reproduce")
    git(root, "init", "-q")
    (root / ".gitignore").write_text("/*\n!/.gitignore\n!/uv.lock\n")
    (root / "uv.lock").write_text("version = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture(scope="module")
def cfg(root: Path) -> AppConfig:
    return config(root)


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    ticks_dir = tmp_path_factory.mktemp("reproduce_ticks")
    ticks = dense_ticks("2024-03-03 22:00", "2024-03-23 00:00", seed=43, mean_interval_s=10)
    write_mt5(ticks, ticks_dir / "XAUUSD_three_weeks.csv")
    engine = validated_pipeline(cfg, ticks_dir)
    registry.add_hypothesis_version(
        engine, "H-0001", title="baseline board", family_id="baselines", yaml_text="x\n"
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def dataset(cfg: AppConfig, engine: Engine) -> DatasetRef:
    spec = dataset_spec(
        target_set={"name": "fwd_returns", "version": "v1"},
        start="2024-03-05T00:00:00Z",
        end="2024-03-22T12:00:00Z",
        context_timeframes=["1h", "4h", "1d"],
    )
    return build_dataset(cfg, engine, spec, git_sha="t")


def xq(root: Path, *args: str) -> tuple[int, str]:
    common = [
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
    ]
    result = CliRunner().invoke(app, [*common, *args])
    return result.exit_code, result.output


@pytest.fixture(scope="module")
def board_run(root: Path, dataset: DatasetRef, engine: Engine) -> str:
    board_file = root / "board.yaml"
    board_file.write_text(yaml.safe_dump(BOARD, sort_keys=False))
    code, output = xq(
        root,
        "baselines",
        "run",
        "--dataset",
        dataset.dataset_id,
        "--config",
        str(board_file),
        "--exploratory",
        "--seed",
        "11",
    )
    assert code == 0, output
    (run,) = [r for r in registry.list_runs(engine) if r.kind == "baseline_board"]
    return run.run_id


def test_a_board_run_reproduces_within_tolerance(
    root: Path, cfg: AppConfig, engine: Engine, dataset: DatasetRef, board_run: str
) -> None:
    trials_before = trial_count(cfg, engine, "baselines").n_trials
    code, output = xq(root, "exp", "reproduce", board_run, "--exploratory")
    assert code == 0, output
    assert f"{board_run} (baseline_board) -> reproduction" in output
    assert ": REPRODUCED;" in output
    assert "provenance" not in output  # the same commit, configuration and lockfile
    assert f"dataset {dataset.dataset_id} rebuilt with identical content" in output
    (copy,) = [r for r in registry.list_runs(engine) if r.kind == REPRODUCTION_KIND]
    original = registry.get_run(engine, board_run)
    assert copy.git_sha == original.git_sha == git(root, "rev-parse", "HEAD")
    (artifact,) = [
        a for a in registry.list_artifacts(engine, copy.run_id) if a.kind == "reproduction"
    ]
    status = json.loads(Path(artifact.path).read_text())
    assert (status["status"], status["counts_as_reproduced"]) == ("REPRODUCED", True)
    assert all(field["matches"] for field in status["identity"].values())
    assert copy.config["run"]["reproduces"] == board_run
    assert copy.config["run"]["run"] == original.config["run"]
    assert (copy.seed, copy.dataset_id) == (original.seed, original.dataset_id)
    # the same configurations on the same data are not new trials
    assert trial_count(cfg, engine, "baselines").n_trials == trials_before
    metrics = {m.name: m.value for m in registry.get_metrics(engine, board_run)}
    again = {m.name: m.value for m in registry.get_metrics(engine, copy.run_id)}
    assert any(name.endswith("/sharpe_p") for name in metrics)
    for name, value in metrics.items():
        assert again[name] == pytest.approx(value, rel=1e-9, abs=1e-12), name


def test_a_deleted_dataset_is_rebuilt_from_its_recorded_spec(
    root: Path, cfg: AppConfig, dataset: DatasetRef, board_run: str
) -> None:
    directory = datasets_root(cfg) / dataset.dataset_id
    manifest = json.loads((directory / "manifest.json").read_text())
    shutil.rmtree(directory)
    code, output = xq(root, "exp", "reproduce", board_run, "--exploratory")
    assert code == 0, output
    assert ": REPRODUCED;" in output
    rebuilt = json.loads((directory / "manifest.json").read_text())
    assert rebuilt["sha256"] == manifest["sha256"]  # the same content from the recorded spec


def test_a_changed_result_is_not_reproduced(root: Path, engine: Engine, board_run: str) -> None:
    name = "board/buy_and_hold/sharpe"
    with session_factory(engine)() as session:
        (original,) = session.scalars(
            select(Metric.value).where(Metric.run_id == board_run, Metric.name == name)
        ).all()
        session.execute(
            update(Metric)
            .where(Metric.run_id == board_run, Metric.name == name)
            .values(value=original + 0.1)
        )
        session.commit()
    try:
        code, output = xq(root, "exp", "reproduce", board_run, "--exploratory")
    finally:
        with session_factory(engine)() as session:
            session.execute(
                update(Metric)
                .where(Metric.run_id == board_run, Metric.name == name)
                .values(value=original)
            )
            session.commit()
    assert code == 1, output
    assert ": NOT_REPRODUCED;" in output  # the same code: a genuine failure to reproduce
    assert f"mismatch: {name}" in output
    assert sum(line.startswith("mismatch:") for line in output.splitlines()) == 1


def test_altered_dataset_content_is_refused(
    root: Path, cfg: AppConfig, dataset: DatasetRef, board_run: str
) -> None:
    features = datasets_root(cfg) / dataset.dataset_id / "features.parquet"
    content = features.read_bytes()
    features.write_bytes(content + b"tampered")
    try:
        code, output = xq(root, "exp", "reproduce", board_run, "--exploratory")
    finally:
        features.write_bytes(content)
    assert code == 2, output
    assert dataset.dataset_id in output


def test_unfinished_runs_and_kinds_without_a_reproducer_are_refused(
    cfg: AppConfig, engine: Engine, board_run: str
) -> None:
    experiment = registry.get_run(engine, board_run).experiment_id
    common = {
        "confirmatory": False,
        "git_sha": "abc",
        "config_hash": "h",
        "config": {},
        "dataset_id": None,
        "lock_hash": "l",
        "seed": 1,
        "host": "test",
    }
    eda = registry.start_run(engine, experiment, kind="eda", **common)  # type: ignore[arg-type]
    with pytest.raises(ReproductionError, match="only finished runs"):
        reproduce_run(cfg, engine, eda.run_id, exploratory=True)
    registry.finish_run(engine, eda.run_id, RunStatus.FINISHED)
    with pytest.raises(ReproductionError, match="no reproducer"):
        reproduce_run(cfg, engine, eda.run_id, exploratory=True)


def test_a_rerun_on_another_commit_is_not_counted_as_reproduced(
    root: Path, engine: Engine, board_run: str
) -> None:
    before = git(root, "rev-parse", "HEAD")
    (root / ".gitignore").write_text("/*\n!/.gitignore\n!/uv.lock\n# a later commit\n")
    git(root, "commit", "-q", "-am", "the code moved on")
    after = git(root, "rev-parse", "HEAD")
    try:
        code, output = xq(root, "exp", "reproduce", board_run, "--exploratory")
    finally:
        git(root, "reset", "-q", "--hard", before)
    assert code == 3, output
    assert ": RERUN_DIFFERENT_CODE;" in output
    assert "not counted as reproduced" in output
    assert f"provenance differs: git_sha {before} -> {after}" in output
    assert "mismatch:" not in output  # the metrics agree, and it is still not a reproduction
    status_file = next(line for line in output.splitlines() if line.startswith("status file:"))
    status = json.loads(Path(status_file.removeprefix("status file: ")).read_text())
    assert status["status"] == "RERUN_DIFFERENT_CODE"
    assert status["counts_as_reproduced"] is False
    assert status["identity"]["git_sha"] == {
        "original": before,
        "reproduction": after,
        "matches": False,
    }


def test_a_rerun_with_another_configuration_or_dirty_tree_is_not_reproduced(
    root: Path, board_run: str
) -> None:
    code, output = xq(
        root, "--set", "logging.level=WARNING", "exp", "reproduce", board_run, "--exploratory"
    )
    assert code == 3, output
    assert ": RERUN_DIFFERENT_CODE;" in output
    assert "provenance differs: app_config_hash" in output
    lock = root / "uv.lock"
    lock.write_text("version = 1\n# edited, not committed\n")
    try:
        code, output = xq(root, "exp", "reproduce", board_run, "--exploratory")
    finally:
        git(root, "checkout", "-q", "--", "uv.lock")
    assert code == 3, output
    assert "+dirty" in output  # a dirty tree identifies no commit
    assert "provenance differs: lock_hash" in output
