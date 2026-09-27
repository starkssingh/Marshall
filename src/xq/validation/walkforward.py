"""Walk-forward runner (WF-002).

`walk_forward` fits, selects and predicts fold by fold on in-memory data:

1. folds come from `WalkForwardSplitter` (purged by ``label_end``, ADR 0027);
2. every candidate of the model's grid is fitted on the fold's training rows and scored on its
   validation rows (mean squared error, or log loss for classification); the best is kept (the
   first on ties). With one candidate or no validation rows there is nothing to select;
3. the selected candidate, fitted on the training rows, predicts the test rows.

Each fold gets its own seed, derived from the run seed and the fold id, so results do not depend on
the order or the process in which folds run: ``n_jobs > 1`` runs folds in separate processes
(``spawn``) and gives the same predictions as a serial run. A fold's output can be cached under a
key covering the model configuration and code version, the fold's boundaries, its seed and a
digest of the rows it reads, so a cache hit can only return what the same computation would.

`run_walk_forward` runs a model on one target of a stored dataset inside an experiment run: it
applies the target schema guard to the feature matrix, stores the predictions through the
prediction store (WF-003, which refuses leaked rows), records one ``fold_results`` row per fold
(fold-level results are always kept, never only the stitched aggregate), logs the stitched
metrics, and records the evaluation as a trial on test folds (EXP-004).
"""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.errors import NaiveTimestampError
from xq.core.seeds import derive_seed, make_rng
from xq.models.base import ModelConfig, ModelSpec, Task, model_targets
from xq.validation.forecast_eval import (
    classification_metrics,
    log_loss,
    mse,
    regression_metrics,
)
from xq.validation.splitters import Fold, WalkForwardConfig, WalkForwardSplitter

if TYPE_CHECKING:
    from xq.tracking.runs import RunContext

#: Columns of a walk-forward prediction frame (indexed by ``decision_time``).
PREDICTION_COLUMNS = ("fold_id", "y_true", "y_pred", "p_raw", "p_cal", "train_end")
CACHE_DIR = "cache/walkforward"


@dataclass(frozen=True)
class FoldResult:
    """What happened in one fold."""

    fold_id: str
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    n_train: int
    n_val: int
    n_test: int
    selected: dict[str, Any]
    validation_losses: list[float]
    metrics: dict[str, float]
    cached: bool = False


@dataclass(frozen=True)
class WalkForwardOutput:
    """Stitched out-of-sample predictions and per-fold results."""

    predictions: pd.DataFrame
    folds: list[FoldResult]


@dataclass(frozen=True)
class WalkForwardResult:
    """A walk-forward evaluation recorded in an experiment run."""

    evaluation: str
    model_hash: str
    predictions: pd.DataFrame
    folds: list[FoldResult]
    metrics: dict[str, float]
    predictions_path: Path
    trial_id: str | None = None


@dataclass(frozen=True)
class _FoldTask:
    fold: Fold
    x_train: pd.DataFrame
    y_train: pd.Series
    x_val: pd.DataFrame
    y_val: pd.Series
    x_test: pd.DataFrame
    y_test: pd.Series
    spec: ModelSpec
    candidates: list[dict[str, Any]]
    seed: int
    train_start: pd.Timestamp


def walk_forward(
    features: pd.DataFrame,
    y: pd.Series,
    label_end: pd.Series,
    spec: ModelSpec,
    config: ModelConfig,
    splitter: WalkForwardConfig,
    *,
    seed: int,
    n_jobs: int = 1,
    cache_dir: Path | None = None,
) -> WalkForwardOutput:
    """Fit, select and predict fold by fold (see the module docstring).

    Args:
        features: Feature matrix indexed by tz-aware decision time; the model reads
            ``config.features`` from it.
        y: Target values on the same index (missing where there is no label).
        label_end: Each target's ``label_end`` (for purging).
        spec: The model family.
        config: Its configuration (parameters, grid, features).
        splitter: Walk-forward settings.
        seed: Base seed; fold seeds are derived from it and the fold id.
        n_jobs: Processes to run folds in (1 = in this process).
        cache_dir: Directory for cached fold outputs, or None to always compute.
    """
    index = pd.DatetimeIndex(features.index)
    if index.tz is None:
        raise NaiveTimestampError("features must be indexed by tz-aware decision times")
    if not (index.equals(pd.DatetimeIndex(y.index)) and index.equals(label_end.index)):
        raise ValueError("features, y and label_end must share one index")
    missing = [c for c in config.features if c not in features.columns]
    if missing:
        raise KeyError(f"feature columns not in the dataset: {missing}")
    x = features.loc[:, list(config.features)]
    target = model_targets(y, spec.task)
    known = target.notna().to_numpy()
    candidates = config.candidates()
    model_hash = config.config_hash(spec)

    folds = WalkForwardSplitter(splitter).split(index, label_end)
    outputs: list[tuple[pd.DataFrame, FoldResult] | None] = [None] * len(folds)
    pending: list[tuple[int, _FoldTask, str | None]] = []
    for i, fold in enumerate(folds):
        train = fold.train_idx[known[fold.train_idx]]
        val = fold.val_idx[known[fold.val_idx]]
        task = _FoldTask(
            fold=fold,
            x_train=x.iloc[train],
            y_train=target.iloc[train],
            x_val=x.iloc[val],
            y_val=target.iloc[val],
            x_test=x.iloc[fold.test_idx],
            y_test=target.iloc[fold.test_idx],
            spec=spec,
            candidates=candidates,
            seed=derive_seed(seed, "fold", fold.fold_id),
            train_start=index[train[0]] if len(train) else fold.test_start,
        )
        if len(train) == 0:
            continue  # every training row lacks a target value
        key = _cache_key(model_hash, task) if cache_dir is not None else None
        hit = _read_cache(cache_dir, key) if cache_dir is not None and key else None
        if hit is not None:
            outputs[i] = hit
        else:
            pending.append((i, task, key))

    for (i, _, key), output in zip(
        pending, _execute([t for _, t, _ in pending], n_jobs), strict=True
    ):
        outputs[i] = output
        if cache_dir is not None and key is not None:
            _write_cache(cache_dir, key, output)

    done = [o for o in outputs if o is not None]
    frames = [frame for frame, _ in done]
    predictions = pd.concat(frames) if frames else _empty_predictions()
    return WalkForwardOutput(predictions, [result for _, result in done])


def run_walk_forward(
    run: RunContext,
    dataset_id: str,
    target: str,
    spec: ModelSpec,
    config: ModelConfig,
    splitter: WalkForwardConfig,
    *,
    evaluation: str | None = None,
    n_jobs: int = 1,
    use_cache: bool = True,
    record_trial: bool = True,
) -> WalkForwardResult:
    """Walk-forward evaluation of `config` on `target` of dataset `dataset_id` inside `run`.

    Args:
        evaluation: Label of this evaluation within the run (default ``<model>:<target>``).
        record_trial: Record the evaluation as one trial on test folds. A caller that turns the
            forecasts into a strategy and records that as the trial passes False.

    Raises:
        TargetLeakError: if a configured feature column is a target column.
    """
    from xq.datasets.builder import load_dataset, read_manifest
    from xq.targets.base import check_feature_matrix, target_values
    from xq.tracking import registry
    from xq.validation.predictions import write_predictions

    cfg = run.cfg
    features = load_dataset(cfg, dataset_id, "features")
    targets = load_dataset(cfg, dataset_id, "targets")
    check_feature_matrix(features.loc[:, list(config.features)], targets["target"].unique())
    one = target_values(targets, target).reindex(features.index)
    label = evaluation or f"{config.name}:{target}"
    model_hash = config.config_hash(spec)
    cache = cfg.paths.resolve(cfg.paths.data_dir) / CACHE_DIR if use_cache else None
    output = walk_forward(
        features,
        one["value"],
        one["label_end"],
        spec,
        config,
        splitter,
        seed=derive_seed(run.run.seed, "walk_forward", label),
        n_jobs=n_jobs,
        cache_dir=cache,
    )
    feature_set = read_manifest(cfg, dataset_id)["spec"]["feature_set"]
    path = write_predictions(
        run,
        label,
        output.predictions,
        embargo=pd.Timedelta(splitter.embargo),
        model_version=f"{spec.name}@{spec.code_version}:{model_hash}",
        feature_set_version=f"{feature_set['name']}.{feature_set['version']}",
    )
    metrics = forecast_metrics(output.predictions, spec.task)
    registry.record_fold_results(run.engine, run.run_id, label, output.folds)
    for name, value in metrics.items():
        run.log_metric(f"{label}/{name}", value)
    trial_id = None
    if record_trial:
        experiment = registry.get_experiment(run.engine, run.run.experiment_id)
        hypothesis = registry.get_hypothesis(
            run.engine, experiment.hypothesis_id, experiment.hypothesis_version
        )
        trial_id = run.record_trial(
            family_id=hypothesis.family_id,
            config={
                "dataset_id": dataset_id,
                "target": target,
                "model": config.model_dump(mode="json"),
                "model_hash": model_hash,
                "splitter": splitter.model_dump(mode="json"),
            },
            evaluated_on_test=True,
        )
    return WalkForwardResult(
        label, model_hash, output.predictions, output.folds, metrics, path, trial_id
    )


def forecast_metrics(predictions: pd.DataFrame, task: Task) -> dict[str, float]:
    """Summary metrics (BASE-006) of predictions with a known ``y_true``.

    Regression: ``n``, ``mse``, ``mae``, ``hit_rate`` and ``mean_pred``. Classification: ``n``,
    ``log_loss``, ``brier``, ``ece``, ``auc`` and ``accuracy``. NaN when undefined.
    """
    known = predictions.loc[predictions["y_true"].notna()]
    y = known["y_true"].to_numpy(np.float64)
    if task == "regression":
        return {"n": float(len(y)), **regression_metrics(y, known["y_pred"].to_numpy(np.float64))}
    p = known["p_raw"].to_numpy(np.float64)
    return {"n": float(len(y)), **classification_metrics(y, p)}


def _fit_fold(task: _FoldTask) -> tuple[pd.DataFrame, FoldResult]:
    spec = task.spec
    losses: list[float] = []
    best = 0
    if len(task.candidates) > 1 and len(task.y_val):
        for params in task.candidates:
            estimator = spec.factory(params)
            estimator.fit(task.x_train, task.y_train, make_rng(task.seed))
            losses.append(_loss(spec.task, task.y_val, estimator.predict(task.x_val)))
        best = int(np.argmin([loss if math.isfinite(loss) else math.inf for loss in losses]))
    estimator = spec.factory(task.candidates[best])
    estimator.fit(task.x_train, task.y_train, make_rng(task.seed))
    raw = np.asarray(estimator.predict(task.x_test), dtype=np.float64)
    if raw.shape != (len(task.x_test),):
        raise ValueError(f"{spec.name} returned {raw.shape} forecasts for {len(task.x_test)} rows")
    fold = task.fold
    classification = spec.task == "classification"
    frame = pd.DataFrame(
        {
            "fold_id": fold.fold_id,
            "y_true": task.y_test.to_numpy(np.float64),
            "y_pred": (raw > 0.5).astype(np.float64) if classification else raw,
            "p_raw": raw if classification else np.nan,
            "p_cal": np.nan,
            "train_end": fold.train_end,
        },
        index=pd.DatetimeIndex(task.x_test.index, name="decision_time"),
    )
    result = FoldResult(
        fold_id=fold.fold_id,
        train_start=task.train_start,
        train_end=fold.train_end,
        test_start=fold.test_start,
        test_end=fold.test_end,
        n_train=len(task.y_train),
        n_val=len(task.y_val),
        n_test=len(task.x_test),
        selected=task.candidates[best],
        validation_losses=losses,
        metrics=forecast_metrics(frame, spec.task),
    )
    return frame, result


def _execute(tasks: Sequence[_FoldTask], n_jobs: int) -> list[tuple[pd.DataFrame, FoldResult]]:
    if n_jobs < 1:
        raise ValueError("n_jobs must be at least 1")
    if n_jobs == 1 or len(tasks) <= 1:
        return [_fit_fold(task) for task in tasks]
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=min(n_jobs, len(tasks)), mp_context=context) as pool:
        return list(pool.map(_fit_fold, tasks))


def _loss(task: Task, y: pd.Series, pred: npt.NDArray[np.float64]) -> float:
    """Validation loss for selection: MSE, or log loss for classification."""
    actual = y.to_numpy(np.float64)
    forecast = np.asarray(pred, dtype=np.float64)
    return mse(actual, forecast) if task == "regression" else log_loss(actual, forecast)


def _cache_key(model_hash: str, task: _FoldTask) -> str:
    digest = hashlib.sha256()
    for part in (task.x_train, task.y_train, task.x_val, task.y_val, task.x_test, task.y_test):
        digest.update(pd.util.hash_pandas_object(part, index=True).to_numpy().tobytes())
    fold = task.fold
    payload = {
        "model": model_hash,
        "candidates": task.candidates,
        "fold": [fold.fold_id, str(fold.train_end), str(fold.test_start), str(fold.test_end)],
        "seed": task.seed,
        "rows": digest.hexdigest(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_cache(directory: Path, key: str) -> tuple[pd.DataFrame, FoldResult] | None:
    meta, frame_path = directory / f"{key}.json", directory / f"{key}.parquet"
    if not (meta.is_file() and frame_path.is_file()):
        return None
    data = json.loads(meta.read_text(encoding="utf-8"))
    for name in ("train_start", "train_end", "test_start", "test_end"):
        data[name] = pd.Timestamp(data[name])
    frame = pd.read_parquet(frame_path)
    frame.index = pd.DatetimeIndex(frame.index, name="decision_time")
    return frame, FoldResult(**{**data, "cached": True})


def _write_cache(directory: Path, key: str, output: tuple[pd.DataFrame, FoldResult]) -> None:
    frame, result = output
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{key}.{os.getpid()}.parquet"
    frame.to_parquet(tmp)
    tmp.replace(directory / f"{key}.parquet")
    data = {**result.__dict__, "cached": False}
    for name in ("train_start", "train_end", "test_start", "test_end"):
        data[name] = str(data[name])
    tmp_meta = directory / f".{key}.{os.getpid()}.json"
    tmp_meta.write_text(json.dumps(data, sort_keys=True, default=str), encoding="utf-8")
    tmp_meta.replace(directory / f"{key}.json")  # written last: it marks the entry complete


def _empty_predictions() -> pd.DataFrame:
    frame = pd.DataFrame({c: pd.Series(dtype="float64") for c in PREDICTION_COLUMNS})
    frame["fold_id"] = frame["fold_id"].astype("object")
    frame["train_end"] = pd.Series(dtype="datetime64[ns, UTC]")
    frame.index = pd.DatetimeIndex([], tz="UTC", name="decision_time")
    return frame


__all__ = [
    "PREDICTION_COLUMNS",
    "FoldResult",
    "WalkForwardOutput",
    "WalkForwardResult",
    "forecast_metrics",
    "run_walk_forward",
    "walk_forward",
]
