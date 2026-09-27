"""STAT-006 and VOL-005 inside an experiment run: every (model, horizon) evaluated on test folds is
a trial of its own model family — `linear_forecasts` or `volatility_models` — never of a
trading-strategy family, so recording them leaves the `baselines` family's trial count and
effective N unchanged (ADR 0046). STAT-006's benchmarks are references and are not counted."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine

from helpers.pipeline import REPO, config
from helpers.simulate import ar1, bar_frame, forward_target, garch_periods
from xq.core.config import AppConfig, ArmaSpec
from xq.core.types import Timeframe
from xq.research.stats.arima import HorizonTarget, arma_study
from xq.research.volatility.benchmarks import benchmark_forecasters
from xq.research.volatility.evaluate import evaluate_forecasters
from xq.tracking import registry
from xq.tracking.db import create_db_engine, upgrade_to_head
from xq.tracking.runs import RunContext, experiment_run
from xq.tracking.trials import (
    LINEAR_FORECAST_FAMILY,
    MODEL_FAMILIES,
    VOLATILITY_MODEL_FAMILY,
    trial_count,
)
from xq.validation.splitters import WalkForwardConfig

STRATEGY_FAMILY = "baselines"


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    # the worst case: the studies run under a trading-strategy hypothesis
    registry.add_hypothesis_version(
        engine, "H-0001", title="baseline board", family_id=STRATEGY_FAMILY, yaml_text="x\n"
    )
    yield engine
    engine.dispose()


def _arma_study(run: RunContext) -> int:
    frame = bar_frame(0.001 * ar1(4000, 0.2, seed=2))
    horizons = []
    for label, bars in (("15m", 1), ("1h", 4)):
        y, ends = forward_target(frame, bars)
        columns = ("open", "close") if bars == 1 else None
        horizons.append(HorizonTarget(label, y, ends, bars, columns))
    study = arma_study(
        frame,
        horizons,
        {"ar1": ArmaSpec(p=1), "arma11": ArmaSpec(p=1, q=1)},
        ["zero_return", "random_walk"],
        WalkForwardConfig(min_train="20D", test_len="10D"),
        alpha=0.05,
        seed=run.run.seed,
        run=run,
    )
    # the random walk is not applicable at 1h here (no bar of the horizon): one comparison fewer
    assert len(study.comparisons) == 2 * 2 + 2 * 1
    return 2 * 2  # two models at two horizons


def _volatility_board(cfg: AppConfig, run: RunContext) -> int:
    periods, _ = garch_periods(1500, omega=0.05, alpha=0.08, beta=0.9, seed=3)
    vol = cfg.volatility_config()
    factories = benchmark_forecasters(vol, Timeframe("1d"))
    board = evaluate_forecasters(
        periods,
        factories,
        1,
        WalkForwardConfig(min_train="800D", test_len="200D"),
        vol.evaluation,
        default=vol.selection.default,
        seed=run.run.seed,
        run=run,
    )
    assert set(board.metrics["model"]) == set(factories)
    return len(factories)


def test_arma_study_records_one_trial_per_model_and_horizon_in_its_family(
    cfg: AppConfig, engine: Engine
) -> None:
    with experiment_run(cfg, engine, "H-0001", {}, kind="stats", seed=1, exploratory=True) as run:
        expected = _arma_study(run)
    counted = trial_count(cfg, engine, LINEAR_FORECAST_FAMILY)
    assert counted.n_trials == counted.n_test_evaluations == expected
    assert trial_count(cfg, engine, STRATEGY_FAMILY).n_trials == 0


def test_the_volatility_board_records_one_trial_per_model_in_its_family(
    cfg: AppConfig, engine: Engine
) -> None:
    with experiment_run(cfg, engine, "H-0001", {}, kind="vol", seed=1, exploratory=True) as run:
        expected = _volatility_board(cfg, run)
    counted = trial_count(cfg, engine, VOLATILITY_MODEL_FAMILY)
    assert counted.n_trials == counted.n_test_evaluations == expected == 4
    assert trial_count(cfg, engine, STRATEGY_FAMILY).n_trials == 0


def test_model_trials_leave_the_strategy_family_unchanged(cfg: AppConfig, engine: Engine) -> None:
    index = pd.date_range("2024-01-01 22:00", periods=200, freq="D", tz="UTC")
    rng = np.random.default_rng(0)
    common = rng.normal(0.0, 0.01, len(index))
    with experiment_run(
        cfg, engine, "H-0001", {}, kind="baseline", seed=1, exploratory=True
    ) as run:
        for i in range(3):  # two near-duplicates and one independent strategy
            noise = rng.normal(0.0, 0.001 if i < 2 else 0.01, len(index))
            returns = pd.Series((common if i < 2 else 0.0) + noise, index=index)
            run.record_trial(
                family_id=STRATEGY_FAMILY,
                config={"strategy": i},
                evaluated_on_test=True,
                sharpe=0.1 * i,
                returns=returns,
            )
    before = trial_count(cfg, engine, STRATEGY_FAMILY)
    everything_before = trial_count(cfg, engine)
    assert (before.n_trials, before.effective_n) == (3, 2)
    with experiment_run(cfg, engine, "H-0001", {}, kind="models", seed=2, exploratory=True) as run:
        added = _arma_study(run) + _volatility_board(cfg, run)
    after = trial_count(cfg, engine, STRATEGY_FAMILY)
    assert after == before  # count, test evaluations, effective N and Sharpe variance
    assert trial_count(cfg, engine).n_trials == everything_before.n_trials + added
    assert STRATEGY_FAMILY not in MODEL_FAMILIES
