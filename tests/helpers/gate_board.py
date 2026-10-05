"""A synthetic world for the release-gate tests (Sprint 13): a clean git repository, three weeks of
synthetic ticks that span a test vault start, a quality run that grades the vault days too, a
dataset that ends before the vault, and a confirmatory baseline board run on it. Synthetic data
only: an engineering check of the procedures, never evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import REPO, config
from helpers.ticks import dense_ticks, write_mt5
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.datasets.builder import DatasetRef, build_dataset
from xq.quality.validate import validate_source
from xq.tracking import registry

#: The test vault starts at the trading day of Monday 18 March 2024 (17:00 New York, EDT).
VAULT_START = "2024-03-18T21:00:00Z"
VAULT_QUALITY_RUN = "01QRUN0000000000000000VLT1"
HYPOTHESIS = "H-0001"
FAMILY = "gate_test"
BOARD: dict[str, Any] = {
    "targets": ["fwd_ret_mid_1h"],
    "walk_forward": {"min_train": "5D", "test_len": "2D", "embargo": "1h"},
    "forecast_baselines": ["zero_return", "historical_mean"],
    "signal_timeframes": ["1h", "4h"],
    "rules": {
        "buy_and_hold": {"rule": "buy_and_hold"},
        "tsmom_8": {"rule": "time_series_momentum", "params": {"lookback": 8}},
    },
    "vol_target": {"annual_vol": 0.10, "lookback": 12, "max_exposure": 2.0},
    "random_entry_seeds": 4,
}
#: Fewer Monte Carlo paths, noise draws and size-check families, for speed (never the gates).
FAST = {
    "validation.monte_carlo.n_paths": 100,
    "validation.noise.n_seeds": 2,
    "validation.spa_size_check.n_sim": 60,
}


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )  # fmt: skip
    return completed.stdout.strip()


def clean_repo(root: Path) -> None:
    """A clean git repository whose runs write only ignored files, so confirmatory runs work."""
    git(root, "init", "-q")
    (root / ".gitignore").write_text("/*\n!/.gitignore\n!/uv.lock\n")
    (root / "uv.lock").write_text("version = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")


def gate_config(root: Path) -> AppConfig:
    return config(root, **{"vault.start": VAULT_START, **FAST})


def xq(root: Path, *args: str) -> tuple[int, str]:
    common = [
        "--config-dir", str(REPO / "config"),
        "--set", f"paths.root={root}",
        "--set", f"paths.migrations_dir={REPO / 'migrations'}",
        "--set", "logging.console=false",
        "--set", "logging.file=null",
        "--set", f"vault.start={VAULT_START}",
    ]  # fmt: skip
    for key, value in FAST.items():
        common += ["--set", f"{key}={value}"]
    result = CliRunner().invoke(app, [*common, *args])
    return result.exit_code, result.output


def build_world(root: Path, ticks_dir: Path) -> tuple[AppConfig, Engine, DatasetRef, str]:
    """The pipeline, the vault quality run, the dataset and a confirmatory board run."""
    clean_repo(root)
    cfg = gate_config(root)
    ticks = dense_ticks("2024-02-25 22:00", "2024-03-23 00:00", seed=41, mean_interval_s=10)
    write_mt5(ticks, ticks_dir / "XAUUSD_three_weeks.csv")
    engine = validated_pipeline(cfg, ticks_dir)
    validate_source(
        cfg,
        engine,
        "mt5_primary",
        run_id=VAULT_QUALITY_RUN,
        git_sha="test",
        include_vault=True,
        vault_access_confirmed=True,
    )
    registry.add_hypothesis_version(
        engine, HYPOTHESIS, title="release-gate test board", family_id=FAMILY, yaml_text="x\n"
    )
    spec = dataset_spec(
        target_set={"name": "fwd_returns", "version": "v1"},
        start="2024-03-05T00:00:00Z",
        end="2024-03-15T12:00:00Z",
        context_timeframes=["1h", "4h", "1d"],
    )
    dataset = build_dataset(cfg, engine, spec, git_sha="t")
    board_file = root / "board.yaml"
    board_file.write_text(yaml.safe_dump(BOARD, sort_keys=False))
    code, output = xq(
        root, "baselines", "run", "--dataset", dataset.dataset_id, "--config", str(board_file),
        "--hypothesis", HYPOTHESIS,
    )  # fmt: skip
    assert code == 0, output
    (run,) = [r for r in registry.list_runs(engine) if r.kind == "baseline_board"]
    assert run.confirmatory
    return cfg, engine, dataset, run.run_id


def register(root: Path, run_id: str, strategy: str) -> str:
    code, output = xq(
        root, "registry", "register", "--run", run_id, "--strategy", strategy, "--actor", "tester"
    )
    assert code == 0, output
    return output.split()[1]
