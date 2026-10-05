"""WF-005 end to end: `xq exp wf-report` on a baseline board run on synthetic ticks reports every
fold of a rule and of a forecast-sign strategy, their fold Sharpe distribution and the decay
regression, from the run's stored returns and folds. Synthetic data: never evidence."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import REPO, config
from helpers.ticks import dense_ticks, write_mt5
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.datasets.builder import build_dataset
from xq.tracking import registry
from xq.validation.walkforward_report import board_report

TEMPLATE = REPO / "experiments" / "hypotheses" / "TEMPLATE.yaml"
BOARD: dict[str, Any] = {
    "targets": ["fwd_ret_mid_1h"],
    "walk_forward": {"min_train": "5D", "test_len": "2D", "embargo": "1h"},
    "forecast_baselines": ["zero_return", "historical_mean"],
    "signal_timeframes": ["1h"],
    "rules": {"tsmom_8": {"rule": "time_series_momentum", "params": {"lookback": 8}}},
    "vol_target": {"annual_vol": 0.10, "lookback": 12, "max_exposure": 2.0},
    "random_entry_seeds": 4,
}


def xq(root: Path, *args: str) -> tuple[int, str]:
    common = [
        "--config-dir", str(REPO / "config"),
        "--set", f"paths.root={root}",
        "--set", f"paths.migrations_dir={REPO / 'migrations'}",
        "--set", "logging.console=false",
        "--set", "logging.file=null",
    ]  # fmt: skip
    result = CliRunner().invoke(app, [*common, *args])
    return result.exit_code, result.output


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("wf_report")


@pytest.fixture(scope="module")
def cfg(root: Path) -> AppConfig:
    return config(root)


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    ticks_dir = tmp_path_factory.mktemp("wf_report_ticks")
    ticks = dense_ticks("2024-02-25 22:00", "2024-03-23 00:00", seed=43, mean_interval_s=10)
    write_mt5(ticks, ticks_dir / "XAUUSD_three_weeks.csv")
    engine = validated_pipeline(cfg, ticks_dir)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def run_id(root: Path, cfg: AppConfig, engine: Engine) -> str:
    data: dict[str, Any] = yaml.safe_load(TEMPLATE.read_text())
    data.update({"id": "H-0901", "family": "wf_report"})
    hypothesis = root / "H-0901.yaml"
    hypothesis.write_text(yaml.safe_dump(data, sort_keys=False))
    assert xq(root, "exp", "register", str(hypothesis))[0] == 0
    spec = dataset_spec(
        target_set={"name": "fwd_returns", "version": "v1"},
        start="2024-03-05T00:00:00Z",
        end="2024-03-22T12:00:00Z",
        context_timeframes=["1h", "4h", "1d"],
    )
    dataset = build_dataset(cfg, engine, spec, git_sha="t")
    board = root / "board.yaml"
    board.write_text(yaml.safe_dump(BOARD, sort_keys=False))
    code, output = xq(
        root, "baselines", "run", "--dataset", dataset.dataset_id, "--config", str(board),
        "--hypothesis", "H-0901", "--exploratory",
    )  # fmt: skip
    assert code == 0, output
    (run,) = [r for r in registry.list_runs(engine) if r.kind == "baseline_board"]
    return run.run_id


def test_a_rule_is_reported_fold_by_fold(root: Path, engine: Engine, run_id: str) -> None:
    runs_before = len(registry.list_runs(engine))
    code, output = xq(root, "exp", "wf-report", run_id, "--strategy", "tsmom_8@1h")
    assert code == 0, output
    assert "decay: one-sided p = " in output
    assert len(registry.list_runs(engine)) == runs_before  # a report, not a run
    path = root / "reports" / "walkforward" / run_id / "tsmom_8_at_1h.json"
    payload = json.loads(path.read_text())
    returns = pd.read_parquet(
        next(
            a.path for a in registry.list_artifacts(engine, run_id) if a.kind == "baseline_returns"
        )
    )
    folds = payload["folds"]
    assert len(folds) >= 4
    assert sum(f["days"] for f in folds) == len(returns)  # every OOS day in exactly one fold
    assert sum(f["net_return"] for f in folds) == pytest.approx(returns["tsmom_8@1h"].sum())
    windows = {
        (r.fold_id, r.test_start.isoformat(), r.test_end.isoformat())
        for r in registry.get_fold_results(engine, run_id)
    }
    assert {(f["fold_id"], f["test_start"], f["test_end"]) for f in folds} <= windows
    defined = sum(f["sharpe"] is not None for f in folds)
    assert payload["fold_sharpe"]["n_folds"] == defined
    assert payload["fold_sharpe"]["undefined"] == len(folds) - defined
    assert payload["decay"]["n_folds"] == len(folds)
    text = path.with_suffix(".md").read_text()
    assert "screening, placeholder costs" in text
    assert "## Decay regression" in text


def test_a_forecast_sign_strategy_carries_its_fold_metrics(engine: Engine, run_id: str) -> None:
    report = board_report(engine, run_id, "historical_mean:fwd_ret_mid_1h", periods_per_year=252)
    assert {"mse", "hit_rate", "n_train"} <= set(report.folds.columns)
    rows = registry.get_fold_results(engine, run_id, "historical_mean:fwd_ret_mid_1h")
    by_fold = {r.fold_id: r.metrics["mse"] for r in rows}
    for fold_id, mse in zip(report.folds["fold_id"], report.folds["mse"], strict=True):
        assert mse == pytest.approx(by_fold[fold_id])


def test_unknown_strategies_and_runs_are_refused(root: Path, run_id: str) -> None:
    code, output = xq(root, "exp", "wf-report", run_id, "--strategy", "nope")
    assert code == 2
    assert "no strategy 'nope'" in output
    code, output = xq(root, "exp", "wf-report", "01UNKNOWNRUN0000000000000", "--strategy", "x")
    assert code == 2
