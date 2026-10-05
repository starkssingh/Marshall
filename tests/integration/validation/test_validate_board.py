"""`xq validate-strategy` on a baseline board run (the board's subject adapter, ADR 0059),
end to end on synthetic ticks: one strategy of the board is validated at a time; a rule's numeric
constants are perturbed (C-25 (4)), or its neighbourhood is not applicable when the hypothesis
declares them fixed a priori; a forecast-sign strategy's neighbourhood is not evaluated; the
rebuilt returns must equal the recorded ones, and altered artifacts are refused. Rules are
screened over the full history and judged on the out-of-sample days (C-15, ADR 0061). Synthetic
data: an engineering check, never evidence."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import REPO, config
from helpers.ticks import dense_ticks, write_mt5
from xq.backtest.costs import SCREENING_LABEL
from xq.backtest.metrics import return_metrics
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.datasets.builder import DatasetRef, build_dataset
from xq.robustness.subject import TrialSummary
from xq.tracking import registry
from xq.tracking.trials import trial_count
from xq.validation.strategy import VALIDATION_KIND
from xq.validation.subjects import board_subject

TEMPLATE = REPO / "experiments" / "hypotheses" / "TEMPLATE.yaml"
FWD = {"name": "fwd_returns", "version": "v1"}
BOARD = {
    "targets": ["fwd_ret_mid_1h"],
    "walk_forward": {"min_train": "5D", "test_len": "2D", "embargo": "1h"},
    "forecast_baselines": ["zero_return", "historical_mean"],
    "signal_timeframes": ["1h", "4h"],
    "rules": {
        "buy_and_hold": {"rule": "buy_and_hold"},
        "tsmom_8": {"rule": "time_series_momentum", "params": {"lookback": 8}},
        "ma_4_12": {"rule": "ma_crossover", "params": {"fast": 4, "slow": 12}},
    },
    "vol_target": {"annual_vol": 0.10, "lookback": 12, "max_exposure": 2.0},
    "random_entry_seeds": 4,
}
FAST = (
    "validation.monte_carlo.n_paths=100",
    "validation.noise.n_seeds=2",
    "validation.spa_size_check.n_sim=60",
)
SOURCE = "a published rule, fixed before any data was seen"


def xq(root: Path, *args: str) -> tuple[int, str]:
    common = [
        "--config-dir", str(REPO / "config"),
        "--set", f"paths.root={root}",
        "--set", f"paths.migrations_dir={REPO / 'migrations'}",
        "--set", "logging.console=false",
        "--set", "logging.file=null",
    ]  # fmt: skip
    for item in FAST:
        common += ["--set", item]
    result = CliRunner().invoke(app, [*common, *args])
    return result.exit_code, result.output


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("validate_board")


@pytest.fixture(scope="module")
def cfg(root: Path) -> AppConfig:
    return config(root)


def hypothesis(root: Path, hypothesis_id: str, family: str, **extra: Any) -> None:
    data: dict[str, Any] = yaml.safe_load(TEMPLATE.read_text())
    data.update({"id": hypothesis_id, "family": family, "slices": ["year"], **extra})
    path = root / f"{hypothesis_id}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    code, output = xq(root, "exp", "register", str(path))
    assert code == 0, output


@pytest.fixture(scope="module")
def engine(
    cfg: AppConfig, root: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Engine]:
    ticks_dir = tmp_path_factory.mktemp("validate_board_ticks")
    ticks = dense_ticks("2024-02-25 22:00", "2024-03-23 00:00", seed=41, mean_interval_s=10)
    write_mt5(ticks, ticks_dir / "XAUUSD_three_weeks.csv")
    engine = validated_pipeline(cfg, ticks_dir)
    hypothesis(root, "H-0801", "board_plain")
    hypothesis(root, "H-0802", "board_fixed", parameters_fixed_a_priori=True, source=SOURCE)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def dataset(cfg: AppConfig, engine: Engine) -> DatasetRef:
    spec = dataset_spec(
        target_set=FWD,
        start="2024-03-05T00:00:00Z",
        end="2024-03-22T12:00:00Z",
        context_timeframes=["1h", "4h", "1d"],
    )
    return build_dataset(cfg, engine, spec, git_sha="t")


def board_run(root: Path, dataset: DatasetRef, engine: Engine, hypothesis_id: str) -> str:
    path = root / "board.yaml"
    path.write_text(yaml.safe_dump(BOARD, sort_keys=False))
    code, output = xq(
        root, "baselines", "run", "--dataset", dataset.dataset_id, "--config", str(path),
        "--hypothesis", hypothesis_id, "--exploratory",
    )  # fmt: skip
    assert code == 0, output
    (run,) = [
        r
        for r in registry.list_runs(engine)
        if r.kind == "baseline_board"
        and registry.get_experiment(engine, r.experiment_id).hypothesis_id == hypothesis_id
    ]
    return run.run_id


@pytest.fixture(scope="module")
def plain_run(root: Path, dataset: DatasetRef, engine: Engine) -> str:
    return board_run(root, dataset, engine, "H-0801")


def report_of(engine: Engine, root: Path, run_id: str, strategy: str) -> dict[str, Any]:
    (validation,) = [
        r
        for r in registry.list_runs(engine)
        if r.kind == VALIDATION_KIND
        and r.config["run"]["validates"] == run_id
        and r.config["run"]["strategy"] == strategy
    ]
    path = root / "reports" / "validation" / run_id / validation.run_id / "report.json"
    payload: dict[str, Any] = json.loads(path.read_text())
    return payload


def rob001(payload: dict[str, Any]) -> dict[str, Any]:
    (row,) = [r for r in payload["robustness_results"] if r["test_id"] == "ROB-001"]
    return row


def test_a_board_strategy_is_chosen_by_name(root: Path, plain_run: str) -> None:
    code, output = xq(root, "validate-strategy", plain_run, "--exploratory")
    assert code == 2
    assert "choose one with --strategy" in output
    code, output = xq(
        root, "validate-strategy", plain_run, "--strategy", "no_such_rule", "--exploratory"
    )
    assert code == 2
    assert "has no strategy 'no_such_rule'" in output


def test_a_rule_has_its_constants_perturbed(
    cfg: AppConfig, root: Path, engine: Engine, plain_run: str
) -> None:
    before = trial_count(cfg, engine, "board_plain").n_trials
    code, output = xq(
        root, "validate-strategy", plain_run, "--strategy", "ma_4_12_vol@1h", "--exploratory"
    )
    assert code == 0, output
    assert f"net figures: {SCREENING_LABEL}" in output
    payload = report_of(engine, root, plain_run, "ma_4_12_vol@1h")
    assert payload["cost_basis"] == SCREENING_LABEL
    assert payload["synthetic"] is False  # the adapter cannot tell; the run is exploratory
    row = rob001(payload)
    assert row["params"]["parameter_kind"] == "constants"
    assert row["params"]["parameters"] == {
        "params.fast": 4.0,
        "params.slow": 12.0,
        "vol_target.annual_vol": 0.1,
        "vol_target.lookback": 12.0,
        "vol_target.max_exposure": 2.0,
    }
    assert row["params"]["gated"] is True
    keys = [c["key"] for c in payload["checks"]]
    assert "parameter_neighbourhood.profitable_share_min" in keys  # judged, whatever it gives
    assert (
        payload["not_applicable"]["R2"].get("parameter_neighbourhood.profitable_share_min") is None
    )
    # the board's other strategies are the baselines R1's paired test reads
    assert any(t["test_name"] == "best_baseline" for t in payload["stat_tests"])
    assert trial_count(cfg, engine, "board_plain").n_trials == before  # validation adds none


def test_a_rule_screened_over_its_full_history_is_judged_on_the_test_days(
    cfg: AppConfig, root: Path, engine: Engine, plain_run: str
) -> None:
    strategy = "tsmom_8@1h"
    subject = board_subject(
        cfg,
        engine,
        registry.get_run(engine, plain_run),
        strategy,
        trials=TrialSummary(n_raw=1, n_effective=1.0, sharpe_variance=0.0, gated="raw"),
        slices=None,
        parameters_fixed_a_priori=None,
    )
    (artifact,) = [
        a for a in registry.list_artifacts(engine, plain_run) if a.kind == "baseline_returns"
    ]
    recorded = pd.read_parquet(artifact.path)[strategy]
    np.testing.assert_allclose(subject.returns.to_numpy(), recorded.to_numpy(), atol=1e-9)
    board = json.loads((Path(artifact.path).parent / "board.json").read_text())
    (row,) = [r for r in board["strategies"] if r["strategy"] == strategy]
    first_oos = pd.Timestamp(board["oos_start"])
    assert pd.Timestamp(row["evaluation_start"]) < first_oos
    # trades are the episodes entered on the test days, fewer than over the evaluation period
    assert len(subject.trades)
    assert (pd.DatetimeIndex(subject.trades["entry_time"]) >= first_oos).all()
    assert len(subject.trades) < row["trade_count"]
    # cost stress reads the test days only, like the fold-aligned record
    stress = subject.cost_stress(cfg.gates_config()).table.loc["baseline"]
    assert stress["net_pnl"] == pytest.approx(row["fold_net_pnl"])
    assert stress["net_sharpe"] == pytest.approx(row["fold_sharpe"])
    assert stress["net_sharpe"] == pytest.approx(return_metrics(subject.returns, 252)["sharpe"])
    # the daily sigma-hat is read on the test days' first decisions
    assert subject.sigma_daily is not None
    assert list(subject.sigma_daily.index) == list(subject.returns.index)
    assert subject.sigma_daily.notna().all()


def test_a_forecast_sign_strategy_has_no_neighbourhood_to_evaluate(
    root: Path, engine: Engine, plain_run: str
) -> None:
    strategy = "historical_mean:fwd_ret_mid_1h"
    code, output = xq(root, "validate-strategy", plain_run, "--strategy", strategy, "--exploratory")
    assert code == 0, output
    payload = report_of(engine, root, plain_run, strategy)
    why = payload["not_evaluated"]["R2"]["parameter_neighbourhood.profitable_share_min"]
    assert "does not refit a forecast model" in why
    assert payload["verdicts"]["R2"] in {"incomplete", "fail"}  # never a pass by default


def test_parameters_declared_fixed_a_priori_make_the_neighbourhood_not_applicable(
    root: Path, dataset: DatasetRef, engine: Engine
) -> None:
    run_id = board_run(root, dataset, engine, "H-0802")
    # a rule on the second signal timeframe rebuilds from its own (4h) bars
    code, output = xq(
        root, "validate-strategy", run_id, "--strategy", "tsmom_8@4h", "--exploratory"
    )
    assert code == 0, output
    assert f"not applicable: parameters fixed a priori (source: {SOURCE})" in output
    payload = report_of(engine, root, run_id, "tsmom_8@4h")
    assert "parameter_neighbourhood.profitable_share_min" in payload["not_applicable"]["R2"]
    assert "parameter_neighbourhood.profitable_share_min" not in [
        c["key"] for c in payload["checks"]
    ]
    row = rob001(payload)  # the constants' neighbourhood is still reported ...
    assert row["params"]["parameters"] == {"params.lookback": 8.0}
    assert row["params"]["gated"] is False  # ... not gated
    assert row["passed"] is None
    # every other robustness gate still applies
    assert {"stressed_costs.net_sharpe_min", "execution_delay.net_sharpe_min"} <= {
        c["key"] for c in payload["checks"]
    }


def test_a_rebuild_that_does_not_match_the_record_is_refused(
    root: Path, engine: Engine, plain_run: str
) -> None:
    (artifact,) = [
        a for a in registry.list_artifacts(engine, plain_run) if a.kind == "baseline_returns"
    ]
    path = Path(artifact.path)
    original = path.read_bytes()
    frame = pd.read_parquet(path)
    try:
        frame["tsmom_8@1h"] = frame["tsmom_8@1h"] + 0.001
        frame.to_parquet(path)
        code, output = xq(
            root, "validate-strategy", plain_run, "--strategy", "tsmom_8@1h", "--exploratory"
        )
        assert code == 2
        assert "missing or altered since it was recorded" in output
    finally:
        path.write_bytes(original)
