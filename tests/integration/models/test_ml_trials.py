"""ML-003 inside a run: every configuration the in-fold search evaluates is counted by the trial
counter (not on test), then the chosen model's out-of-sample evaluation counts once on test."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine

from helpers.pipeline import REPO, config
from xq.core.config import AppConfig
from xq.core.seeds import make_rng
from xq.models.hpo import optuna_search, trial_recorder
from xq.models.pipeline import FoldData, run_pipeline
from xq.tracking import registry
from xq.tracking.db import create_db_engine, upgrade_to_head
from xq.tracking.runs import experiment_run
from xq.tracking.trials import trial_count
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter

FAMILY = "ml_stage_a"


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    registry.add_hypothesis_version(engine, "H-0101", title="t", family_id=FAMILY, yaml_text="x\n")
    yield engine
    engine.dispose()


def signal_data(n: int = 1600) -> FoldData:
    rng = make_rng(12)
    times = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    x = pd.DataFrame(rng.normal(size=(n, 3)), index=times, columns=["a", "b", "c"])
    y = pd.Series(((x["a"] + rng.normal(0, 1, n)) > 0).astype(float), index=times)
    return FoldData(x, y, pd.Series(times + pd.Timedelta(hours=2), index=times))


def test_every_evaluated_configuration_is_a_trial(cfg: AppConfig, engine: Engine) -> None:
    data = signal_data()
    folds = WalkForwardSplitter(
        WalkForwardConfig.model_validate({"min_train": "30D", "test_len": "10D", "embargo": "1D"})
    ).split(pd.DatetimeIndex(data.x.index), data.label_end)
    budget = 6
    with experiment_run(cfg, engine, "H-0101", {}, kind="ml", seed=4, exploratory=True) as run:
        record = trial_recorder(run, FAMILY, {"model": "logistic"})
        search = optuna_search(
            "logistic", cfg.ml_config().hpo, seed=run.run.seed, record=record, n_trials=budget
        )
        output = run_pipeline(
            "logistic",
            data,
            folds,
            cfg.ml_config().pipeline,
            embargo=pd.Timedelta(days=1),
            seed=run.run.seed,
            search=search,
        )
        run.record_trial(
            family_id=FAMILY, config={"model": "logistic", "stage": "oos"}, evaluated_on_test=True
        )
    trained = len(output.folds)
    assert trained == len(folds)
    stats = trial_count(cfg, engine, FAMILY)
    assert stats.n_trials == budget * trained + 1
    assert stats.n_test_evaluations == 1
    # the planted signal is found out of sample
    oos = output.predictions.dropna(subset=["y_true", "p_cal"])
    assert np.mean((oos["p_cal"] > 0.5) == (oos["y_true"] == 1)) > 0.6
    for fold in output.folds:
        assert len(fold.inner_losses) <= budget
        assert set(fold.params) >= {"C", "l1_ratio", "max_iter"}
