"""BASE-005 end to end: `xq baselines run` puts every baseline (with BASE-003's AR(1)) through
walk-forward and the cost model on a synthetic dataset and writes a board whose net figures are
marked as screening. C-15 (ADR 0061): rules run on two signal timeframes over the full history
after their own warm-up, with the fold-aligned view of the same returns and descriptive slices."""

import json
from collections.abc import Iterator
from pathlib import Path

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
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.core.errors import ConfigError
from xq.datasets.builder import DatasetRef, build_dataset, load_dataset
from xq.models import board as board_module
from xq.models.board import evaluation_start, load_board_config, rule_signal_bars
from xq.tracking import registry
from xq.tracking.hypotheses import load_hypothesis
from xq.tracking.trials import trial_count

FWD = {"name": "fwd_returns", "version": "v1"}
BOARD = {
    "targets": ["fwd_ret_mid_15m", "fwd_ret_mid_1h"],
    "walk_forward": {"min_train": "5D", "test_len": "2D", "embargo": "1h"},
    "forecast_baselines": ["zero_return", "random_walk", "historical_mean", "climatology", "ar1"],
    "signal_timeframes": ["1h", "4h"],
    "rules": {
        "buy_and_hold": {"rule": "buy_and_hold"},
        "tsmom_8": {"rule": "time_series_momentum", "params": {"lookback": 8}},
        "zscore_12": {
            "rule": "zscore_reversion",
            "params": {"lookback": 12, "entry": 1.5, "exit": 0.0},
        },
        "ma_4_12": {"rule": "ma_crossover", "params": {"fast": 4, "slow": 12}},
        "donchian_8_4": {
            "rule": "donchian_breakout",
            "params": {"entry": 8, "exit": 4, "atr_window": 8, "atr_stop": 2.0},
        },
    },
    "vol_target": {"annual_vol": 0.10, "lookback": 12, "max_exposure": 2.0},
    "random_entry_seeds": 8,
}
RULES = ["buy_and_hold", "tsmom_8", "zscore_12", "ma_4_12", "donchian_8_4"]
TIMEFRAMES = ["1h", "4h"]
RULE_STRATEGIES = [f"{r}@{tf}" for tf in TIMEFRAMES for r in RULES + [f"{r}_vol" for r in RULES]]
#: Warm-up bars of each rule (lookbacks count bars of the signal timeframe; the volatility target
#: needs 13 bars for its 12 returns).
WARMUPS = {"buy_and_hold": 1, "tsmom_8": 9, "zscore_12": 12, "ma_4_12": 12, "donchian_8_4": 9}
#: The hypothesis text the board's run reads its declared slices from.
HYPOTHESIS_TEXT = "id: H-0001\nslices:\n  - year\n  - session\n"
FORECAST_STRATEGIES = [
    f"{name}:{target}"
    for target in BOARD["targets"]
    for name in ("random_walk", "historical_mean", "climatology", "ar1")
]


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("board")


@pytest.fixture(scope="module")
def cfg(root: Path) -> AppConfig:
    return config(root)


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    ticks_dir = tmp_path_factory.mktemp("board_ticks")
    ticks = dense_ticks("2024-03-03 22:00", "2024-03-23 00:00", seed=41, mean_interval_s=10)
    write_mt5(ticks, ticks_dir / "XAUUSD_three_weeks.csv")
    engine = validated_pipeline(cfg, ticks_dir)
    registry.add_hypothesis_version(
        engine, "H-0001", title="baseline board", family_id="baselines", yaml_text=HYPOTHESIS_TEXT
    )
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


@pytest.fixture(scope="module")
def board_file(root: Path) -> Path:
    path = root / "board.yaml"
    path.write_text(yaml.safe_dump(BOARD, sort_keys=False))
    return path


def run_board(
    root: Path,
    dataset: DatasetRef,
    board_file: Path,
    seed: int = 5,
    *,
    hypothesis: str = "H-0001",
    expect_exit: int = 0,
) -> str:
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
    result = CliRunner().invoke(
        app,
        [
            *common,
            "baselines",
            "run",
            "--dataset",
            dataset.dataset_id,
            "--config",
            str(board_file),
            "--exploratory",
            "--seed",
            str(seed),
            "--hypothesis",
            hypothesis,
        ],
    )
    assert result.exit_code == expect_exit, result.output
    return result.output if expect_exit == 0 else f"{result.output}{result.exception}"


@pytest.fixture(scope="module")
def first_run(root: Path, dataset: DatasetRef, board_file: Path, engine: Engine) -> str:
    return run_board(root, dataset, board_file)


def report_dirs(root: Path, dataset: DatasetRef) -> list[Path]:
    """The report directories of the board runs under H-0001 (family ``baselines``)."""
    base = root / "reports" / "baselines" / dataset.dataset_id
    return sorted(
        p
        for p in base.iterdir()
        if p.is_dir()
        and json.loads((p / "board.json").read_text())["hypothesis_family"] == "baselines"
    )


def test_one_command_writes_the_board(root: Path, dataset: DatasetRef, first_run: str) -> None:
    (directory,) = report_dirs(root, dataset)
    assert {p.name for p in directory.iterdir()} == {
        "board.md",
        "board.json",
        "returns.parquet",
        "returns_evaluation.parquet",
    }
    board = json.loads((directory / "board.json").read_text())
    names = [row["strategy"] for row in board["strategies"]]
    assert names == FORECAST_STRATEGIES + RULE_STRATEGIES
    assert board["signal_timeframes"] == TIMEFRAMES
    assert board["cost_basis"] == SCREENING_LABEL
    assert board["folds"] >= 3
    assert board["oos_days"] >= 6
    # every net figure is marked, in the JSON rows, the Markdown table and the CLI output
    assert all(row["cost_basis"] == SCREENING_LABEL for row in board["strategies"])
    table = [line for line in (directory / "board.md").read_text().splitlines() if "|" in line]
    strategy_rows = [line for line in table if line.startswith(("| buy", "| tsmom", "| ma_"))]
    assert strategy_rows
    assert all(line.rstrip(" |").endswith(SCREENING_LABEL) for line in strategy_rows)
    printed = [line for line in first_run.splitlines() if line.startswith("  ")]
    assert len(printed) == len(names)
    assert all(line.endswith(f"({SCREENING_LABEL})") for line in printed)


def test_every_strategy_is_a_trial_and_has_intervals(
    cfg: AppConfig, engine: Engine, root: Path, dataset: DatasetRef, first_run: str
) -> None:
    (directory,) = report_dirs(root, dataset)
    board = json.loads((directory / "board.json").read_text())
    stats = trial_count(cfg, engine, "baselines")
    assert stats.n_trials == len(board["strategies"]) == board["trials"]["raw"]
    assert stats.n_test_evaluations == stats.n_trials
    assert 1 <= stats.effective_n <= stats.n_trials
    hold = next(r for r in board["strategies"] if r["strategy"] == "buy_and_hold@1h")
    assert hold["sharpe_ci_low"] <= hold["sharpe"] <= hold["sharpe_ci_high"]
    assert hold["annual_return_ci_low"] <= hold["annual_return"] <= hold["annual_return_ci_high"]
    assert 0 <= hold["sharpe_p"] <= 1
    assert 0 <= hold["dsr"] <= 1
    assert hold["block_length"] >= 5  # the gates' minimum block length
    assert hold["trade_count"] == 0  # one open trade, never closed
    assert hold["financing"] > 0  # held over rollovers
    assert hold["closed_market_decisions"] == 0  # a constant target never re-trades
    assert hold["period"] == "full_history"
    assert hold["warmup_bars"] == 1
    # forecast rows: every baseline on both targets; DM against zero_return where it applies
    forecasts = {(r["target"], r["baseline"]): r for r in board["forecasts"]}
    assert set(forecasts) == {(t, b) for t in BOARD["targets"] for b in BOARD["forecast_baselines"]}
    mean = forecasts[("fwd_ret_mid_1h", "historical_mean")]
    assert mean["mean_loss_ci_low"] <= mean["mean_loss"] <= mean["mean_loss_ci_high"]
    assert 0 <= mean["dm_vs_zero_p"] <= 1
    assert forecasts[("fwd_ret_mid_1h", "zero_return")]["dm_vs_zero_p"] is None
    ar1 = forecasts[("fwd_ret_mid_1h", "ar1")]  # BASE-003: the AR baseline is on the board
    assert ar1["n"] > 0
    assert 0 <= ar1["dm_vs_zero_p"] <= 1


def test_daily_returns_cover_every_out_of_sample_day(
    root: Path, dataset: DatasetRef, first_run: str
) -> None:
    (directory,) = report_dirs(root, dataset)
    board = json.loads((directory / "board.json").read_text())
    returns = pd.read_parquet(directory / "returns.parquet")  # the fold-aligned view
    assert list(returns.columns) == [row["strategy"] for row in board["strategies"]]
    assert len(returns) == board["oos_days"]
    assert not returns.isna().any().any()
    for row in board["strategies"]:
        assert row["fold_net_pnl"] == pytest.approx(returns[row["strategy"]].sum() * 100_000)
        if row["period"] == "test_folds":  # a forecast-sign strategy is judged on the folds
            assert row["net_pnl"] == pytest.approx(row["fold_net_pnl"])
            assert row["sharpe"] == row["fold_sharpe"]


def test_rules_are_evaluated_over_the_full_history_after_their_warm_up(
    cfg: AppConfig, root: Path, dataset: DatasetRef, first_run: str
) -> None:
    (directory,) = report_dirs(root, dataset)
    board = json.loads((directory / "board.json").read_text())
    features = load_dataset(cfg, dataset.dataset_id, "features")
    decisions = pd.DatetimeIndex(features.index)
    evaluation = pd.read_parquet(directory / "returns_evaluation.parquet")
    assert list(evaluation.columns) == [row["strategy"] for row in board["strategies"]]
    assert len(evaluation) == board["history_days"] > board["oos_days"]
    assert board["history_start"] == str(decisions[0])
    for row in board["strategies"]:
        column = evaluation[row["strategy"]]
        start = pd.Timestamp(row["evaluation_start"])
        if row["period"] == "test_folds":
            assert start == pd.Timestamp(board["oos_start"])
            assert row["signal_timeframe"] is None
            assert row["warmup_bars"] is None
        else:
            name, timeframe = row["strategy"].split("@")
            warmup = max(WARMUPS[name.removesuffix("_vol")], 13 if name.endswith("_vol") else 0)
            assert (row["signal_timeframe"], row["warmup_bars"]) == (timeframe, warmup)
            bars, _ = rule_signal_bars(cfg, features, timeframe, 252)
            ready = bars.index[warmup - 1]  # the warm-up bar's availability
            assert start == decisions[decisions >= ready][0]
            assert start == evaluation_start(row["strategy"], bars, warmup, decisions)
            assert start < pd.Timestamp(board["oos_start"])  # longer than the test folds
            if warmup > 1:
                assert start > decisions[0]
        # the evaluation period is the trailing run of days from its start; missing before it
        assert column.notna().sum() == row["evaluation_days"]
        assert column.notna().to_numpy()[::-1].cumprod().sum() == row["evaluation_days"]
        assert row["net_pnl"] == pytest.approx(column.sum() * 100_000)
    assert {r["strategy"] for r in board["strategies"] if r["period"] == "full_history"} == set(
        RULE_STRATEGIES
    )


def test_the_fold_aligned_view_is_the_full_history_on_the_test_days(
    root: Path, dataset: DatasetRef, first_run: str
) -> None:
    (directory,) = report_dirs(root, dataset)
    folds = pd.read_parquet(directory / "returns.parquet")
    evaluation = pd.read_parquet(directory / "returns_evaluation.parquet")
    on_test_days = evaluation.loc[folds.index]
    # every strategy's evaluation period covers every out-of-sample day here
    assert not on_test_days.isna().any().any()
    np.testing.assert_array_equal(on_test_days.to_numpy(), folds.to_numpy())


def test_slices_by_year_and_session_are_descriptive(
    root: Path, dataset: DatasetRef, first_run: str
) -> None:
    (directory,) = report_dirs(root, dataset)
    board = json.loads((directory / "board.json").read_text())
    slices = board["slices"]
    assert slices["declared"] == ["year", "session"]
    assert slices["label"] == "descriptive (not tested: no p-values, no trials)"
    assert slices["error"] is None
    assert set(slices["strategies"]) == {row["strategy"] for row in board["strategies"]}
    for row in board["strategies"]:
        tables = slices["strategies"][row["strategy"]]
        assert set(tables) == {"year", "session"}
        (year,) = tables["year"]["rows"]  # three weeks of 2024
        assert (year["bucket"], year["days"]) == ("2024", row["evaluation_days"])
        assert year["net_pnl"] == pytest.approx(row["net_pnl"])
        assert all("p_value" not in r for t in tables.values() for r in t["rows"])
        trades = sum(r["trades"] for r in tables["session"]["rows"])
        assert trades == row["trade_count"]  # closed trades entered in the evaluation period
    assert any(slices["strategies"][r]["session"]["rows"] for r in RULE_STRATEGIES)
    text = (directory / "board.md").read_text()
    assert "## Slices — descriptive (not tested: no p-values, no trials)" in text
    assert "### By year — descriptive" in text
    assert "### By session — descriptive" in text


def test_the_board_regenerates_identically(
    root: Path, dataset: DatasetRef, board_file: Path, first_run: str
) -> None:
    run_board(root, dataset, board_file)
    first, second = (
        json.loads((d / "board.json").read_text()) for d in report_dirs(root, dataset)[:2]
    )
    for a, b in zip(first["strategies"], second["strategies"], strict=True):
        for key in ("sharpe", "sharpe_ci_low", "sharpe_ci_high", "sharpe_p", "net_pnl"):
            assert a[key] == b[key], (a["strategy"], key)
        # the trial count grew, so the deflation is stricter the second time
        if a["dsr"] is not None:
            assert b["dsr"] <= a["dsr"] + 1e-12
    assert second["trials"]["raw"] == 2 * first["trials"]["raw"]


#: A second hypothesis in its own family, for runs that must not touch the board's trials.
OTHER = "H-0099"


@pytest.fixture(scope="module")
def other_hypothesis(engine: Engine) -> str:
    registry.add_hypothesis_version(
        engine,
        OTHER,
        title="slice and warm-up checks",
        family_id="board_checks",
        yaml_text="id: H-0099\nslices:\n  - year\n  - trend/range regime\n",
    )
    return OTHER


def small_board(root: Path, name: str, **changes: object) -> Path:
    board = {
        **BOARD,
        "forecast_baselines": [],
        "rules": {"tsmom_8": BOARD["rules"]["tsmom_8"]},  # type: ignore[index]
        "random_entry_seeds": 0,
        **changes,
    }
    path = root / f"{name}.yaml"
    path.write_text(yaml.safe_dump(board, sort_keys=False))
    return path


def test_a_slice_that_cannot_be_computed_is_reported_not_fatal(
    root: Path, dataset: DatasetRef, first_run: str, other_hypothesis: str
) -> None:
    run_board(root, dataset, small_board(root, "regime"), hypothesis=other_hypothesis)
    base = root / "reports" / "baselines" / dataset.dataset_id
    (directory,) = [
        p
        for p in base.iterdir()
        if json.loads((p / "board.json").read_text())["hypothesis_family"] == "board_checks"
    ]
    board = json.loads((directory / "board.json").read_text())
    assert board["slices"]["declared"] == ["year", "trend_range_regime"]
    for row in board["strategies"]:
        assert "REG-007" in board["slices"]["strategies"][row["strategy"]]["error"]
    assert "slices not computed" in (directory / "board.md").read_text()


def test_a_dataset_too_short_for_a_rule_s_warm_up_is_refused(
    root: Path, dataset: DatasetRef, first_run: str, other_hypothesis: str
) -> None:
    long = {"tsmom_400": {"rule": "time_series_momentum", "params": {"lookback": 400}}}
    path = small_board(root, "too_short", rules=long)
    output = run_board(root, dataset, path, hypothesis=other_hypothesis, expect_exit=2)
    assert "too short for rule tsmom_400@1h" in output


def test_a_position_before_the_warm_up_ends_stops_the_board(
    root: Path,
    dataset: DatasetRef,
    first_run: str,
    other_hypothesis: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A warm-up formula two bars too long: the rule trades before its evaluation would start.
    real = board_module.rule_warmup
    monkeypatch.setattr(board_module, "rule_warmup", lambda rule, target: real(rule, target) + 2)
    output = run_board(
        root, dataset, small_board(root, "guard"), hypothesis=other_hypothesis, expect_exit=2
    )
    assert "the warm-up formula is wrong" in output


def test_repository_board_is_the_plan_s_fixed_baselines() -> None:
    board = load_board_config(REPO / "experiments" / "configs" / "baselines" / "board.yaml")
    strategies = {name: entry.rule for name, entry in board.strategies().items()}
    assert strategies["ma_20_50@1d"].params == {"fast": 20, "slow": 50}
    assert strategies["ma_50_200@1h"].params == {"fast": 50, "slow": 200}
    assert {s.rule for s in strategies.values()} == {
        "buy_and_hold",
        "time_series_momentum",
        "zscore_reversion",
        "ma_crossover",
        "donchian_breakout",
    }
    assert sum(s.vol_target for s in strategies.values()) == len(board.rules) * 2
    assert board.random_entry_seeds == 1000
    assert set(board.forecast_baselines) == {
        "zero_return",
        "random_walk",
        "historical_mean",
        "climatology",
    }
    assert np.isclose(board.ci_level, 0.95)


#: The revised H-0001 draft (ADR 0035): rule baselines on 1d and 1h signal bars (C-15, ADR 0061).
H0001_RULE_TIMEFRAMES = ["1d", "1h"]
H0001 = REPO / "experiments" / "hypotheses" / "H-0001.yaml"
PENDING_WINDOW = "set from the real data's depth at registration"


def test_h0001_draft_cannot_be_registered_until_its_windows_are_set() -> None:
    # The windows are set from the real data's depth at registration (ADR 0035); until then the
    # draft must not validate, so `xq exp register` refuses it.
    with pytest.raises(ConfigError, match="discovery_window"):
        load_hypothesis(H0001, config(REPO))


def test_h0001_draft_is_a_valid_preregistration_once_its_windows_are_set(tmp_path: Path) -> None:
    text = H0001.read_text(encoding="utf-8")
    assert text.count(PENDING_WINDOW) == 2
    filled = text.replace(
        f"discovery_window: {PENDING_WINDOW}",
        'discovery_window: {start: "2021-09-26T21:00:00Z", end: "2024-09-25T21:00:00Z"}',
    ).replace(
        f"evaluation_window: {PENDING_WINDOW}",
        'evaluation_window: {start: "2024-09-25T21:00:00Z", end: "2025-09-25T21:00:00Z"}',
    )
    path = tmp_path / "H-0001.yaml"
    path.write_text(filled, encoding="utf-8")
    doc, _ = load_hypothesis(path, config(REPO))
    assert (doc.id, doc.family) == ("H-0001", "baselines")
    assert doc.slices == ["year", "session"]
    board = load_board_config(REPO / "experiments" / "configs" / "baselines" / "board.yaml")
    assert board.signal_timeframes == H0001_RULE_TIMEFRAMES
    rule_strategies = len(board.strategies())
    forecast_strategies = len(board.targets) * (len(board.forecast_baselines) - 1)
    assert doc.trial_budget == rule_strategies + forecast_strategies


def test_the_repository_board_has_exactly_h0001_s_36_strategies() -> None:
    # 6 rules x plain and volatility-targeted x 1d and 1h = 24 rule trials, plus 4 targets x
    # random_walk, historical_mean and climatology = 12 forecast-sign trials (zero_return is the
    # reference, never a strategy). The fold-aligned view is not a trial (ADR 0041).
    board = load_board_config(REPO / "experiments" / "configs" / "baselines" / "board.yaml")
    rules = board.strategies()
    assert len(rules) == 24
    assert {name.split("@")[1] for name in rules} == {"1d", "1h"}
    assert "tsmom_252@1d" in rules
    assert "tsmom_252_vol@1h" in rules
    forecast_sign = [b for b in board.forecast_baselines if b != "zero_return"]
    assert len(rules) + len(board.targets) * len(forecast_sign) == 36
