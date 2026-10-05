"""The in-fold machine-learning pipeline (ML-002).

`train_fold` trains one model on one walk-forward fold, using only that fold's training window:

1. **Rows.** A row is usable when its target is known and every input is finite (nothing is
   filled). The fold's training window is its purged training (and validation) rows.
2. **Validation split.** The last ``validation_fraction`` of the window's rows (the plan: 20 %)
   calibrate; the fitting rows before them are purged against the validation start minus the
   embargo (by ``label_end``, or ``max(label_end, weight_end)`` with uniqueness weights, C-30 (4)).
3. **Sample weights** (``uniqueness``): average uniqueness (TGT-006) of the fitting rows' labels
   among themselves, scaled to a mean of 1; computed on the fitting rows only.
4. **Hyperparameters.** Given, or chosen by a `Search` (ML-003) minimizing `inner_cv_loss`: the
   mean loss (log loss, or MSE for regression) over a **purged k-fold with embargo** inside the
   fitting rows, with a scaler fitted on each inner training split alone.
5. **Refit** on every fitting row, after a `TrainingFoldScaler` fitted on those rows (a column
   constant there carries no information and is set to 0).
6. **Calibration** on the validation rows only (`fit_calibrator`: isotonic above
   ``isotonic_min_samples`` rows, Platt otherwise), weighted, with ``uniqueness`` weights, by the
   validation labels' raw average uniqueness among themselves: overlapping validation labels
   count as the few independent outcomes they are.
7. **Test.** The fold's test rows with finite inputs get ``p_raw`` and ``p_cal`` (classification)
   or ``y_pred``; others stay missing. ``train_end`` is the latest purge end of any row used.

`run_pipeline` runs every fold with a seed derived from the run seed and the fold id and stitches
the test predictions into one out-of-sample series (`stitch_oos`). Test rows never fit, select,
stop, scale or calibrate anything. Shuffled splits do not exist here; the purging demonstration
(``tests/unit/models/test_pipeline.py``) shows why.

The embargo is the plan's ``max(label horizon, 1 trading day)`` (`default_embargo`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import MlPipelineConfig, SessionsConfig
from xq.core.seeds import derive_seed
from xq.data.calendar import regular_trading_day
from xq.features.base import TrainingFoldScaler
from xq.models.base import SklearnForecaster, Task, build_forecaster, forecaster_spec
from xq.models.calibration import Calibrator, fit_calibrator
from xq.targets.weights import label_uniqueness, uniqueness_weights
from xq.validation.forecast_eval import ece, log_loss, mse
from xq.validation.splitters import Fold, PurgedKFold, Split
from xq.validation.walkforward import PREDICTION_COLUMNS, stitch_oos

IntArray = npt.NDArray[np.int64]
FloatArray = npt.NDArray[np.float64]
Objective = Callable[[Mapping[str, Any]], float]
#: ``search(objective, fold_id) -> params``: a hyperparameter search (ML-003).
Search = Callable[[Objective, str], dict[str, Any]]


@dataclass(frozen=True)
class FoldData:
    """The full sample folds index into: model inputs, targets and label ends (one index)."""

    x: pd.DataFrame
    y: pd.Series
    label_end: pd.Series

    def __post_init__(self) -> None:
        index = pd.DatetimeIndex(self.x.index)
        if index.tz is None:
            raise ValueError("model inputs must be indexed by tz-aware decision times")
        if not (index.equals(self.y.index) and index.equals(self.label_end.index)):
            raise ValueError("x, y and label_end must share one index")


@dataclass
class TrainedFold:
    """One fold's model, its transforms and its out-of-sample predictions."""

    fold_id: str
    params: dict[str, Any]
    forecaster: SklearnForecaster
    scaler: TrainingFoldScaler
    calibrator: Calibrator | None
    n_fit: int
    n_val: int
    n_test: int
    train_end: pd.Timestamp
    val_metrics: dict[str, float]
    predictions: pd.DataFrame
    fit_index: pd.DatetimeIndex
    val_index: pd.DatetimeIndex
    inner_losses: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineOutput:
    """Stitched out-of-sample predictions and every trained fold (folds skipped for too few rows
    are listed by id)."""

    predictions: pd.DataFrame
    folds: list[TrainedFold]
    skipped: list[str]


def default_embargo(horizon: pd.Timedelta, sessions: SessionsConfig) -> pd.Timedelta:
    """The plan's embargo: the label horizon or one regular trading day, whichever is longer."""
    return max(pd.Timedelta(horizon), regular_trading_day(sessions))


def usable_rows(data: FoldData) -> npt.NDArray[np.bool_]:
    """Rows with a known target and finite inputs."""
    values = data.x.to_numpy(np.float64)
    finite: npt.NDArray[np.bool_] = np.isfinite(values).all(axis=1)
    known: npt.NDArray[np.bool_] = data.y.notna().to_numpy() & data.label_end.notna().to_numpy()
    return finite & known


def split_validation(
    times: pd.DatetimeIndex,
    purge_end: npt.NDArray[np.int64],
    window: IntArray,
    fraction: float,
    embargo: pd.Timedelta,
) -> tuple[IntArray, IntArray]:
    """The fitting and validation rows of a training window (positions, in time order): the last
    `fraction` of its rows validate; fitting rows end (`purge_end`, int64 ns) before the
    validation start minus `embargo`."""
    window = np.sort(window)
    n_val = max(1, math.ceil(fraction * len(window))) if len(window) else 0
    val = window[len(window) - n_val :]
    if not len(val):
        return window, val
    cutoff = times[val[0]].value - embargo.value
    head = window[: len(window) - n_val]
    return head[purge_end[head] < cutoff], val


def inner_splits(
    times: pd.DatetimeIndex,
    label_end: pd.Series,
    weight_end: pd.Series | None,
    n_splits: int,
    embargo: pd.Timedelta,
) -> list[Split]:
    """Purged k-fold splits with embargo over the fitting rows (positions into them)."""
    return PurgedKFold(n_splits, embargo).split(times, label_end, weight_end=weight_end)


def inner_cv_loss(
    family: str,
    params: Mapping[str, Any],
    x: pd.DataFrame,
    y: pd.Series,
    weights: pd.Series | None,
    splits: Sequence[Split],
    seed: int,
) -> float:
    """Mean loss of `family` with `params` over `splits` of the fitting rows: each split's model
    and scaler see only its own training rows. Splits whose training rows lack a class are left
    out; with none usable the loss is infinite."""
    task = forecaster_spec(family).task
    losses = []
    for split in splits:
        train, test = split.train_idx, split.test_idx
        y_train = y.iloc[train]
        if not len(test) or (task == "classification" and y_train.nunique() < 2):
            continue
        scaler = TrainingFoldScaler().fit(x, train)
        scaled = _scaled(scaler, x)
        model = build_forecaster(family, params, seed).fit(
            scaled.iloc[train],
            y_train,
            sample_weight=None if weights is None else weights.iloc[train],
        )
        losses.append(_loss(task, model, scaled.iloc[test], y.iloc[test]))
    return float(np.mean(losses)) if losses else math.inf


def train_fold(
    family: str,
    fold: Fold,
    data: FoldData,
    cfg: MlPipelineConfig,
    *,
    embargo: pd.Timedelta,
    seed: int,
    params: Mapping[str, Any] | None = None,
    fixed: Mapping[str, Any] | None = None,
    search: Search | None = None,
) -> TrainedFold | None:
    """Train `family` on one fold's training window and predict its test rows (module
    docstring). Returns None when the fold has fewer than ``min_training_rows`` fitting rows.

    Args:
        params: Hyperparameters to use as they are (no search).
        fixed: Parameters held fixed while `search` chooses the others.
        search: A hyperparameter search (ML-003); without one, `params` (or the family's
            defaults) are used.
    """
    spec = forecaster_spec(family)
    times = pd.DatetimeIndex(data.x.index)
    usable = usable_rows(data)
    label_end = _ns(data.label_end)
    window = np.union1d(fold.train_idx, fold.val_idx).astype(np.int64)
    window = window[usable[window]]
    fit, val = split_validation(times, label_end, window, cfg.validation_fraction, embargo)
    if len(fit) < cfg.min_training_rows:
        return None
    x_fit, y_fit = data.x.iloc[fit], data.y.iloc[fit].astype(np.float64)
    if spec.task == "classification" and y_fit.nunique() < 2:
        return None
    weights, weight_end = _weights(cfg, times[fit], data.label_end.iloc[fit])
    held = dict(fixed or {})
    chosen: dict[str, Any]
    inner: dict[str, float] = {}
    if search is not None:
        splits = inner_splits(
            times[fit], data.label_end.iloc[fit], weight_end, cfg.inner_splits, embargo
        )

        def objective(candidate: Mapping[str, Any]) -> float:
            merged = {**held, **candidate}
            loss = inner_cv_loss(family, merged, x_fit, y_fit, weights, splits, seed)
            inner[repr(sorted(merged.items()))] = loss
            return loss

        chosen = {**held, **search(objective, fold.fold_id)}
    else:
        chosen = {**held, **dict(params or {})}

    scaler = TrainingFoldScaler().fit(data.x, fit)
    scaled = _scaled(scaler, data.x)
    model = build_forecaster(family, chosen, seed).fit(
        scaled.iloc[fit], y_fit, sample_weight=weights
    )
    calibrator, val_metrics = _calibrate(spec.task, model, scaled, data, val, cfg, times)
    test = fold.test_idx[usable_rows_inputs(data)[fold.test_idx]]
    predictions = _predict(spec.task, model, calibrator, scaled, data, fold, test)
    used = np.concatenate([fit, val])
    train_end = pd.Timestamp(int(label_end[used].max()), tz="UTC")
    predictions["train_end"] = train_end
    return TrainedFold(
        fold_id=fold.fold_id,
        params=chosen,
        forecaster=model,
        scaler=scaler,
        calibrator=calibrator,
        n_fit=len(fit),
        n_val=len(val),
        n_test=len(test),
        train_end=train_end,
        val_metrics=val_metrics,
        predictions=predictions,
        fit_index=times[fit],
        val_index=times[val],
        inner_losses=inner,
    )


def run_pipeline(
    family: str,
    data: FoldData,
    folds: Sequence[Fold],
    cfg: MlPipelineConfig,
    *,
    embargo: pd.Timedelta,
    seed: int,
    params: Mapping[str, Any] | None = None,
    fixed: Mapping[str, Any] | None = None,
    search: Search | None = None,
) -> PipelineOutput:
    """`train_fold` on every fold (seeds derived per fold), stitched out of sample."""
    trained: list[TrainedFold] = []
    skipped: list[str] = []
    for fold in folds:
        result = train_fold(
            family,
            fold,
            data,
            cfg,
            embargo=embargo,
            seed=derive_seed(seed, "fold", fold.fold_id),
            params=params,
            fixed=fixed,
            search=search,
        )
        if result is None:
            skipped.append(fold.fold_id)
        else:
            trained.append(result)
    by_id = {f.fold_id: f for f in folds}
    windows = [(by_id[t.fold_id].test_start, by_id[t.fold_id].test_end) for t in trained]
    stitched = stitch_oos(
        [t.predictions for t in trained],
        windows,
        pd.DatetimeIndex(data.x.index),
        contiguous=False,
    )
    return PipelineOutput(stitched, trained, skipped)


def usable_rows_inputs(data: FoldData) -> npt.NDArray[np.bool_]:
    """Rows whose inputs are all finite (a test row needs no known target to be predicted)."""
    finite: npt.NDArray[np.bool_] = np.isfinite(data.x.to_numpy(np.float64)).all(axis=1)
    return finite


def _weights(
    cfg: MlPipelineConfig, times: pd.DatetimeIndex, label_end: pd.Series
) -> tuple[pd.Series | None, pd.Series | None]:
    if cfg.sample_weights == "none":
        return None, None
    starts = pd.Series(times, index=times)
    ends = pd.Series(pd.DatetimeIndex(label_end), index=times)
    unique = label_uniqueness(starts, ends)
    weights = uniqueness_weights(unique["uniqueness"]).set_axis(times)
    return weights, unique["weight_end"].set_axis(times)


def _scaled(scaler: TrainingFoldScaler, x: pd.DataFrame) -> pd.DataFrame:
    """Standardized inputs; a column constant in the fitting rows (no scale) becomes 0."""
    out = scaler.transform(x)
    assert scaler.std_ is not None
    constant = [c for c in out.columns if not scaler.std_[c] > 0]
    if constant:
        out[constant] = 0.0
    return out


def _loss(task: Task, model: SklearnForecaster, x: pd.DataFrame, y: pd.Series) -> float:
    actual = y.to_numpy(np.float64)
    if task == "classification":
        return log_loss(actual, model.predict_proba(x))
    return mse(actual, model.predict(x))


def _calibrate(
    task: Task,
    model: SklearnForecaster,
    scaled: pd.DataFrame,
    data: FoldData,
    val: IntArray,
    cfg: MlPipelineConfig,
    times: pd.DatetimeIndex,
) -> tuple[Calibrator | None, dict[str, float]]:
    if task != "classification" or not len(val):
        return None, {}
    y_val = data.y.iloc[val].to_numpy(np.float64)
    p_val = model.predict_proba(scaled.iloc[val])
    metrics = {
        "n": float(len(val)),
        "log_loss_raw": log_loss(y_val, p_val),
        "ece_raw": ece(y_val, p_val),
    }
    if cfg.calibration == "none" or len(np.unique(y_val)) < 2:
        return None, metrics
    weight = None
    if cfg.sample_weights == "uniqueness":
        rows = times[val]
        unique = label_uniqueness(
            pd.Series(rows, index=rows), pd.Series(data.label_end.iloc[val].to_numpy(), index=rows)
        )
        weight = unique["uniqueness"].to_numpy(np.float64)
    calibrator = fit_calibrator(
        p_val,
        y_val,
        cfg.calibration,
        isotonic_min_samples=cfg.isotonic_min_samples,
        sample_weight=weight,
    )
    p_cal = calibrator.transform(p_val)
    metrics |= {"log_loss_cal": log_loss(y_val, p_cal), "ece_cal": ece(y_val, p_cal)}
    return calibrator, metrics


def _predict(
    task: Task,
    model: SklearnForecaster,
    calibrator: Calibrator | None,
    scaled: pd.DataFrame,
    data: FoldData,
    fold: Fold,
    test: IntArray,
) -> pd.DataFrame:
    index = pd.DatetimeIndex(data.x.index[fold.test_idx], name="decision_time")
    frame = pd.DataFrame(
        {
            "fold_id": fold.fold_id,
            "y_true": data.y.iloc[fold.test_idx].to_numpy(np.float64),
            "y_pred": np.nan,
            "p_raw": np.nan,
            "p_cal": np.nan,
            "train_end": pd.NaT,
        },
        index=index,
    )
    if len(test):
        rows = data.x.index[test]
        if task == "classification":
            p = model.predict_proba(scaled.iloc[test])
            frame.loc[rows, "p_raw"] = p
            cal = calibrator.transform(p) if calibrator is not None else p
            frame.loc[rows, "p_cal"] = cal
            frame.loc[rows, "y_pred"] = (cal > 0.5).astype(np.float64)
        else:
            frame.loc[rows, "y_pred"] = model.predict(scaled.iloc[test])
    return frame.loc[:, list(PREDICTION_COLUMNS)]


def _ns(series: pd.Series) -> npt.NDArray[np.int64]:
    values: npt.NDArray[np.int64] = (
        pd.DatetimeIndex(series).as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
    )
    return values
