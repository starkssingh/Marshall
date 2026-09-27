"""STAT-006 inside an experiment run: every (model, horizon) evaluated on test folds is a trial of
the hypothesis family; the benchmarks are references and are not counted."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from helpers.pipeline import REPO, config
from helpers.simulate import ar1, bar_frame, forward_target
from xq.core.config import AppConfig, ArmaSpec
from xq.research.stats.arima import HorizonTarget, arma_study
from xq.tracking import registry
from xq.tracking.db import create_db_engine, upgrade_to_head
from xq.tracking.runs import experiment_run
from xq.tracking.trials import trial_count
from xq.validation.splitters import WalkForwardConfig


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    registry.add_hypothesis_version(
        engine, "H-0009", title="t", family_id="linear_forecasts", yaml_text="x\n"
    )
    yield engine
    engine.dispose()


def test_arma_study_records_one_trial_per_model_and_horizon(cfg: AppConfig, engine: Engine) -> None:
    frame = bar_frame(0.001 * ar1(4000, 0.2, seed=2))
    horizons = []
    for label, bars in (("15m", 1), ("1h", 4)):
        y, ends = forward_target(frame, bars)
        horizons.append(
            HorizonTarget(label, y, ends, bars, ("open", "close") if bars == 1 else None)
        )
    splitter = WalkForwardConfig(min_train="20D", test_len="10D")
    with experiment_run(cfg, engine, "H-0009", {}, kind="stats", seed=1, exploratory=True) as run:
        study = arma_study(
            frame,
            horizons,
            {"ar1": ArmaSpec(p=1), "arma11": ArmaSpec(p=1, q=1)},
            ["zero_return", "random_walk"],
            splitter,
            alpha=0.05,
            seed=run.run.seed,
            run=run,
            family_id="linear_forecasts",
        )
    counted = trial_count(cfg, engine, "linear_forecasts")
    assert counted.n_trials == 4
    assert counted.n_test_evaluations == 4
    # the random walk is not applicable at 1h here (no bar of the horizon): one comparison fewer
    assert len(study.comparisons) == 2 * 2 + 2 * 1


def test_recording_trials_needs_the_family(cfg: AppConfig, engine: Engine) -> None:
    frame = bar_frame(0.001 * ar1(500, 0.2, seed=2))
    y, ends = forward_target(frame, 1)
    with (
        experiment_run(cfg, engine, "H-0009", {}, kind="stats", seed=1, exploratory=True) as run,
        pytest.raises(ValueError, match="family"),
    ):
        arma_study(
            frame,
            [HorizonTarget("15m", y, ends, 1)],
            {"ar1": ArmaSpec(p=1)},
            ["zero_return"],
            WalkForwardConfig(min_train="2D", test_len="1D"),
            alpha=0.05,
            seed=1,
            run=run,
        )
