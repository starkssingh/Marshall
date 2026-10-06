"""C-34 (4) inside a run: the deflated Sharpe ratio's trial count is the number of distinct pipeline
specifications (model family x feature set x target x target-set version) evaluated on outer test
folds. Inner search configurations are recorded per fold (count, seed, chosen parameters) but are
not trials."""

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, select

from helpers.pipeline import REPO, config
from xq.core.config import AppConfig
from xq.core.seeds import derive_seed, make_rng
from xq.models.hpo import PipelineSpec, optuna_search, record_hpo, record_pipeline_trial
from xq.models.persistence import DIAGNOSTIC_LABEL, NotEvidenceError
from xq.models.pipeline import FoldData, PipelineOutput, run_pipeline
from xq.tracking import registry
from xq.tracking.db import create_db_engine, session_factory, upgrade_to_head
from xq.tracking.models import Metric
from xq.tracking.runs import RunContext, experiment_run
from xq.tracking.trials import trial_count
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter

FAMILY = "ml_stage_a"
BUDGET = 6
SPEC = PipelineSpec(
    model="logistic", feature_set="core.v1", target="dir_h2", target_set_version="fwd_returns.v1"
)


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


def searched(cfg: AppConfig, run: RunContext, family: str, data: FoldData) -> PipelineOutput:
    folds = WalkForwardSplitter(
        WalkForwardConfig.model_validate({"min_train": "30D", "test_len": "10D", "embargo": "1D"})
    ).split(pd.DatetimeIndex(data.x.index), data.label_end)
    search = optuna_search(family, cfg.ml_config().hpo, seed=run.run.seed, n_trials=BUDGET)
    return run_pipeline(
        family, data, folds, cfg.ml_config().pipeline, embargo=pd.Timedelta(days=1),
        seed=run.run.seed, search=search,
    )  # fmt: skip


def test_search_configurations_are_recorded_per_fold_but_are_not_trials(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    data = signal_data()
    with experiment_run(cfg, engine, "H-0101", {}, kind="ml", seed=4, exploratory=True) as run:
        output = searched(cfg, run, "logistic", data)
        path = record_hpo(run, output, SPEC, tmp_path / "hpo")
        assert trial_count(cfg, engine, FAMILY).n_trials == 0  # the searches counted nothing
        record_pipeline_trial(run, FAMILY, SPEC)
        run_id = run.run_id
    trained = len(output.folds)
    assert trained >= 3
    stats = trial_count(cfg, engine, FAMILY)
    assert stats.n_trials == 1  # one specification, not BUDGET x folds + 1
    assert stats.n_test_evaluations == 1
    # every fold's search is recorded: its count, its seed and its chosen parameters
    summary = json.loads(path.read_text())
    assert summary["spec"] == SPEC.model_dump()
    assert [f["fold_id"] for f in summary["folds"]] == [f.fold_id for f in output.folds]
    for fold, record in zip(output.folds, summary["folds"], strict=True):
        assert fold.hpo is not None
        assert record["n_configs"] == BUDGET == fold.hpo["n_configs"]
        assert record["seed"] == derive_seed(4, "hpo", "logistic", fold.fold_id)
        assert record["best_params"] == fold.params
        assert set(fold.params) >= {"C", "l1_ratio", "max_iter"}
        assert len(fold.inner_losses) <= BUDGET
    with session_factory(engine)() as session:
        metrics = session.scalars(select(Metric).where(Metric.run_id == run_id)).all()
    per_fold = {(m.name, m.fold_id): m.value for m in metrics}
    for fold in output.folds:
        assert per_fold[("hpo_n_configs", fold.fold_id)] == BUDGET
        assert per_fold[("hpo_seed", fold.fold_id)] == derive_seed(
            4, "hpo", "logistic", fold.fold_id
        )
    assert any(a.kind == "hpo" for a in registry.list_artifacts(engine, run_id))
    # the planted signal is found out of sample
    oos = output.predictions.dropna(subset=["y_true", "p_cal"])
    assert np.mean((oos["p_cal"] > 0.5) == (oos["y_true"] == 1)) > 0.6


def test_the_trial_count_is_the_number_of_distinct_specifications(
    cfg: AppConfig, engine: Engine
) -> None:
    other_model = SPEC.model_copy(update={"model": "random_forest"})
    other_features = SPEC.model_copy(update={"feature_set": "base.v1"})
    other_target = SPEC.model_copy(update={"target": "dir_h4"})
    other_version = SPEC.model_copy(update={"target_set_version": "fwd_returns.v2"})
    with experiment_run(cfg, engine, "H-0101", {}, kind="ml", seed=1, exploratory=True) as run:
        first = record_pipeline_trial(run, FAMILY, SPEC)
        assert record_pipeline_trial(run, FAMILY, SPEC) == first  # evaluated again: not a trial
        for spec in (other_model, other_features, other_target, other_version):
            record_pipeline_trial(run, FAMILY, spec)
    assert trial_count(cfg, engine, FAMILY).n_trials == 5
    # another run of the same specification, with other seeds and hyperparameters: still one
    with experiment_run(cfg, engine, "H-0101", {}, kind="ml", seed=2, exploratory=True) as run:
        assert record_pipeline_trial(run, FAMILY, SPEC) == first
        record_pipeline_trial(run, FAMILY, SPEC.model_copy(update={"model": "ridge"}))
    stats = trial_count(cfg, engine, FAMILY)
    assert stats.n_trials == 6
    assert stats.n_test_evaluations == 6


def test_diagnostic_predictions_are_refused_as_a_trial(cfg: AppConfig, engine: Engine) -> None:
    labelled = pd.DataFrame({"p_cal": [0.5], "label": [DIAGNOSTIC_LABEL]})
    with experiment_run(cfg, engine, "H-0101", {}, kind="ml", seed=3, exploratory=True) as run:
        with pytest.raises(NotEvidenceError):
            record_pipeline_trial(run, FAMILY, SPEC, predictions=labelled)
        record_pipeline_trial(run, FAMILY, SPEC, predictions=labelled.drop(columns="label"))
    assert trial_count(cfg, engine, FAMILY).n_trials == 1
