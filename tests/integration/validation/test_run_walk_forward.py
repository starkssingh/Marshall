"""WF-002 and WF-003 end to end: a model walks forward over a stored dataset inside a run."""

import math
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
from xq.models.baselines import forecast_baseline
from xq.tracking import registry
from xq.tracking.runs import experiment_run
from xq.tracking.trials import trial_count
from xq.validation.predictions import (
    STORE_COLUMNS,
    PredictionLeakError,
    read_predictions,
    write_predictions,
)
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

    stored = read_predictions(result.predictions_path)
    assert result.predictions_path.parent.name == run.run_id
    assert ["decision_time", *stored.columns] == list(STORE_COLUMNS)
    pd.testing.assert_frame_equal(
        stored.drop(columns=["model_version", "feature_set_version"]),
        result.predictions,
        check_freq=False,
    )
    assert stored["feature_set_version"].unique().tolist() == ["base.v1"]
    assert stored["model_version"].iloc[0] == f"mean@1:{result.model_hash}"
    artifacts = registry.list_artifacts(engine, run.run_id)
    assert [a.kind for a in artifacts] == ["predictions"]


def test_the_store_refuses_leaked_rows_and_writes_nothing(
    cfg: AppConfig, engine: Engine, dataset: DatasetRef
) -> None:
    with experiment_run(
        cfg, engine, "H-0001", {}, kind="baseline", seed=11, exploratory=True
    ) as run:
        result = run_walk_forward(
            run,
            dataset.dataset_id,
            "fwd_ret_mid_4h",
            MEAN,
            ModelConfig(name="mean"),
            SPLITS,
            record_trial=False,
        )
        leaked = result.predictions.copy()
        leaked.iloc[0, leaked.columns.get_loc("train_end")] = leaked.index[0]
        with pytest.raises(PredictionLeakError):
            write_predictions(
                run,
                "leaky",
                leaked,
                embargo=pd.Timedelta(0),
                model_version="m",
                feature_set_version="base.v1",
            )
    names = [p.name for p in result.predictions_path.parent.iterdir()]
    assert names == [result.predictions_path.name]
    assert len(registry.list_artifacts(engine, run.run_id)) == 1


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


def test_undefined_metrics_are_kept_but_not_logged(
    cfg: AppConfig, engine: Engine, dataset: DatasetRef
) -> None:
    # A zero forecast has no signed forecast, so its hit rate is undefined (NaN).
    zero = forecast_baseline("zero_return")
    with experiment_run(
        cfg,
        engine,
        "H-0001",
        {},
        kind="baseline",
        seed=12,
        dataset_id=dataset.dataset_id,
        exploratory=True,
    ) as run:
        result = run_walk_forward(
            run,
            dataset.dataset_id,
            "fwd_ret_mid_1h",
            zero,
            ModelConfig(name="zero_return"),
            SPLITS,
            record_trial=False,
        )
    assert math.isnan(result.metrics["hit_rate"])
    logged = {m.name for m in registry.get_metrics(engine, run.run_id)}
    assert "zero_return:fwd_ret_mid_1h/mse" in logged
    assert "zero_return:fwd_ret_mid_1h/hit_rate" not in logged
