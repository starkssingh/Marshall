"""WF-002 end to end: a model walks forward over a stored dataset inside an experiment run."""

from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.models import MEAN
from helpers.pipeline import config
from xq.core.config import AppConfig
from xq.datasets.builder import DatasetRef, build_dataset
from xq.models.base import ModelConfig
from xq.tracking import registry
from xq.tracking.runs import experiment_run
from xq.tracking.trials import trial_count
from xq.validation.splitters import WalkForwardConfig
from xq.validation.walkforward import run_walk_forward

FWD = {"name": "fwd_returns", "version": "v1"}
SPLITS = WalkForwardConfig.model_validate({"min_train": "1D", "test_len": "1D", "embargo": "1h"})


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> AppConfig:
    return config(tmp_path_factory.mktemp("wf"))


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, clean_week_dir: Path) -> Iterator[Engine]:
    engine = validated_pipeline(cfg, clean_week_dir)
    registry.add_hypothesis_version(
        engine, "H-0001", title="baselines", family_id="baselines", yaml_text="x\n"
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def dataset(cfg: AppConfig, engine: Engine) -> DatasetRef:
    return build_dataset(cfg, engine, dataset_spec(target_set=FWD), git_sha="t")


def test_walk_forward_over_a_dataset_is_recorded_fold_by_fold(
    cfg: AppConfig, engine: Engine, dataset: DatasetRef
) -> None:
    with experiment_run(
        cfg,
        engine,
        "H-0001",
        {},
        kind="baseline",
        seed=11,
        dataset_id=dataset.dataset_id,
        exploratory=True,
    ) as run:
        result = run_walk_forward(
            run, dataset.dataset_id, "fwd_ret_mid_1h", MEAN, ModelConfig(name="mean"), SPLITS
        )
    assert result.evaluation == "mean:fwd_ret_mid_1h"
    assert len(result.folds) >= 2
    predictions = result.predictions
    assert (predictions.index > predictions["train_end"] + pd.Timedelta(hours=1)).all()
    rows = registry.get_fold_results(engine, run.run_id)
    assert [r.fold_id for r in rows] == [f.fold_id for f in result.folds]
    assert rows[0].evaluation == result.evaluation
    assert rows[0].metrics["n_test"] == result.folds[0].n_test
    assert rows[0].test_start == result.folds[0].test_start
    metrics = {m.name: m.value for m in registry.get_metrics(engine, run.run_id)}
    assert metrics["mean:fwd_ret_mid_1h/n"] == result.metrics["n"] > 0
    stats = trial_count(cfg, engine, "baselines")
    assert (stats.n_trials, stats.n_test_evaluations) == (1, 1)
    assert result.trial_id is not None


def test_a_rerun_reads_cached_folds_and_records_them_again(
    cfg: AppConfig, engine: Engine, dataset: DatasetRef
) -> None:
    results = []
    for _ in range(2):
        with experiment_run(
            cfg,
            engine,
            "H-0001",
            {},
            kind="baseline",
            seed=11,
            dataset_id=dataset.dataset_id,
            exploratory=True,
        ) as run:
            result = run_walk_forward(
                run,
                dataset.dataset_id,
                "fwd_ret_mid_15m",
                MEAN,
                ModelConfig(name="mean"),
                SPLITS,
                record_trial=False,
            )
        results.append((run.run_id, result))
    (_, first), (second_run, second) = results
    assert not any(f.cached for f in first.folds)
    assert all(f.cached for f in second.folds)  # same seed, same rows: same computation
    pd.testing.assert_frame_equal(first.predictions, second.predictions, check_freq=False)
    rows = registry.get_fold_results(engine, second_run)
    assert [r.fold_id for r in rows] == [f.fold_id for f in second.folds]
    assert second.trial_id is None
