"""The in-fold machine-learning pipeline (ML-002).

`train_fold` trains one model on one walk-forward fold, using only that fold's training window:

1. **Rows.** A row is usable when its target is known and every input is finite (nothing is
   filled). The fold's training window is its purged training (and validation) rows. What is
   left out is counted per fold (`TrainedFold.dropped`, C-34 (2)): window rows with a missing
   input, window rows with an unknown target (inputs finite), usable rows purged between the
   fitting and validation rows, test rows left unpredicted for a missing input, and input
   columns constant in the fitting rows (set to 0).
2. **Validation split.** The last ``validation_fraction`` of the window's rows (the plan: 20 %)
   calibrate; the fitting rows before them are purged against the validation start minus the
   embargo (by ``label_end``, or ``max(label_end, weight_end)`` with uniqueness weights, C-30 (4)).
3. **Sample weights** (``uniqueness``): average uniqueness (TGT-006) of the fitting rows' labels
   among themselves, scaled to a mean of 1; computed on the fitting rows only.
4. **Hyperparameters.** Given, or chosen by a `Search` (ML-003) minimizing `inner_cv_loss`: the
   mean loss (log loss, or MSE for regression) over a **purged k-fold with embargo** inside the
   fitting rows, with a scaler fitted on each inner training split alone. The search is recorded
   per fold (`TrainedFold.hpo`: configurations evaluated, sampler seed, chosen parameters and
   their inner loss); its configurations are not trials (C-34 (4), `xq.models.hpo`).
5. **Refit** on every fitting row, after a `TrainingFoldScaler` fitted on those rows (a column
   constant there carries no information and is set to 0).
6. **Calibration** on the validation rows only (`fit_calibrator`: isotonic above
   ``isotonic_min_samples`` rows, Platt otherwise), weighted, with ``uniqueness`` weights, by the
   validation labels' raw average uniqueness among themselves: overlapping validation labels
   count as the few independent outcomes they are. Then (C-34 (1), ADR 0068):

   - **Shrinkage.** The calibrated probabilities keep ``n_eff / (n_eff + k0)`` of their distance
     from the fold's **training base rate** (the fitting rows' mean label), where ``n_eff`` is
     the validation labels' summed uniqueness (their effective number of independent outcomes)
     and ``k0`` is ``shrinkage_prior``.
   - **No-skill fallback.** The calibrated, shrunk model's validation log loss is estimated
     honestly by **cross-fitting** over a purged k-fold with embargo of the validation rows
     (``k = inner_splits``, the inner search's splitter): each block is scored by the map
     (calibration and shrinkage, with its own ``n_eff``) fitted on the other blocks' rows purged
     against it. Losses are weighted as the calibration is. If that loss is not below the
     training base rate's loss on the same rows, or cannot be estimated (no block whose fitting
     rows hold both classes, or validation rows of one class only), the fold **predicts the base
     rate** (``fallback``). Scoring the map on the
     rows it was fitted on would almost never show "no skill"; cross-fitting avoids that.

   ``base_rate``, ``n_eff``, ``shrinkage`` and ``fallback`` are recorded per fold.
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
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import MlPipelineConfig, SessionsConfig
from xq.core.seeds import derive_seed
from xq.data.calendar import regular_trading_day
from xq.features.base import TrainingFoldScaler
from xq.models.base import SklearnForecaster, Task, build_forecaster, forecaster_spec
from xq.models.calibration import Calibrator, base_rate_map, fit_calibrator
from xq.targets.weights import label_uniqueness, uniqueness_weights
from xq.validation.forecast_eval import ece, log_loss, mse
from xq.validation.splitters import Fold, PurgedKFold, Split
from xq.validation.walkforward import PREDICTION_COLUMNS, stitch_oos

IntArray = npt.NDArray[np.int64]
FloatArray = npt.NDArray[np.float64]
Objective = Callable[[Mapping[str, Any]], float]


@dataclass(frozen=True)
class SearchResult:
    """What a hyperparameter search chose for one fold and how (ML-003, C-34 (4))."""

    params: dict[str, Any]
    #: Configurations evaluated (on the inner purged CV, never on test).
    n_configs: int
    #: The sampler's seed for this fold.
    seed: int
    #: The chosen configuration's inner CV loss.
    best_loss: float


#: ``search(objective, fold_id) -> SearchResult``: a hyperparameter search (ML-003).
Search = Callable[[Objective, str], SearchResult]


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
    #: The fitting rows' mean label (classification), which the calibrated map shrinks towards.
    base_rate: float | None = None
    #: The validation labels' summed uniqueness (their effective number of independent outcomes).
    n_eff: float | None = None
    #: The share of the calibrated distance from the base rate kept (0 under the fallback).
    shrinkage: float | None = None
    #: True when the fold predicts its base rate (no skill shown on validation, C-34 (1)).
    fallback: bool = False
    #: What was left out (module docstring, item 1): ``missing_input``, ``unknown_target``,
    #: ``purged``, ``test_unpredicted`` and ``constant_columns``.
    dropped: dict[str, int] = field(default_factory=dict)
    #: The fold's hyperparameter search (C-34 (4)): ``n_configs``, ``seed``, ``best_params`` and
    #: ``best_inner_loss``; None when the parameters were given.
    hpo: dict[str, Any] | None = None


@dataclass(frozen=True)
class PipelineOutput:
    """Stitched out-of-sample predictions and every trained fold (folds skipped for too few rows
    are listed by id)."""

    predictions: pd.DataFrame
    folds: list[TrainedFold]
    skipped: list[str]

    def fold_table(self) -> pd.DataFrame:
        """One row per trained fold: its row counts, what was dropped (C-34 (2)) and its
        calibration (base rate, shrinkage, fallback; C-34 (1))."""
        rows = [
            {
                "fold_id": f.fold_id,
                "n_fit": f.n_fit,
                "n_val": f.n_val,
                "n_test": f.n_test,
                **{f"dropped_{k}": v for k, v in f.dropped.items()},
                "base_rate": f.base_rate,
                "n_eff": f.n_eff,
                "shrinkage": f.shrinkage,
                "fallback": f.fallback,
            }
            for f in self.folds
        ]
        return pd.DataFrame(rows).set_index("fold_id") if rows else pd.DataFrame()


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
        scaled = scaled_inputs(scaler, x)
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
    finite = usable_rows_inputs(data)
    missing_input = int((~finite[window]).sum())
    unknown_target = int((finite[window] & ~usable[window]).sum())
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
    hpo: dict[str, Any] | None = None
    if search is not None:
        splits = inner_splits(
            times[fit], data.label_end.iloc[fit], weight_end, cfg.inner_splits, embargo
        )

        def objective(candidate: Mapping[str, Any]) -> float:
            merged = {**held, **candidate}
            loss = inner_cv_loss(family, merged, x_fit, y_fit, weights, splits, seed)
            inner[repr(sorted(merged.items()))] = loss
            return loss

        result = search(objective, fold.fold_id)
        chosen = {**held, **result.params}
        hpo = {
            "n_configs": result.n_configs,
            "seed": result.seed,
            "best_params": dict(chosen),
            "best_inner_loss": result.best_loss,
        }
    else:
        chosen = {**held, **dict(params or {})}

    scaler = TrainingFoldScaler().fit(data.x, fit)
    scaled = scaled_inputs(scaler, data.x)
    model = build_forecaster(family, chosen, seed).fit(
        scaled.iloc[fit], y_fit, sample_weight=weights
    )
    calibrator, val_metrics = _calibrate(
        spec.task, model, scaled, data, _Rows(fit, val, times, label_end), cfg, embargo
    )
    test = fold.test_idx[finite[fold.test_idx]]
    assert scaler.std_ is not None
    dropped = {
        "missing_input": missing_input,
        "unknown_target": unknown_target,
        "purged": len(window) - len(fit) - len(val),
        "test_unpredicted": len(fold.test_idx) - len(test),
        "constant_columns": int((~(scaler.std_ > 0)).sum()),
    }
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
        base_rate=val_metrics.get("base_rate"),
        n_eff=val_metrics.get("n_eff"),
        shrinkage=val_metrics.get("shrinkage"),
        fallback=bool(val_metrics.get("fallback", 0.0)),
        dropped=dropped,
        hpo=hpo,
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


def scaled_inputs(scaler: TrainingFoldScaler, x: pd.DataFrame) -> pd.DataFrame:
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


@dataclass(frozen=True)
class _Rows:
    """A fold's fitting and validation rows (positions), with the sample's times and label ends."""

    fit: IntArray
    val: IntArray
    times: pd.DatetimeIndex
    label_end: npt.NDArray[np.int64]


def _calibrate(
    task: Task,
    model: SklearnForecaster,
    scaled: pd.DataFrame,
    data: FoldData,
    rows: _Rows,
    cfg: MlPipelineConfig,
    embargo: pd.Timedelta,
) -> tuple[Calibrator | None, dict[str, float]]:
    val = rows.val
    if task != "classification" or not len(val):
        return None, {}
    y_val = data.y.iloc[val].to_numpy(np.float64)
    p_val = model.predict_proba(scaled.iloc[val])
    metrics = {
        "n": float(len(val)),
        "log_loss_raw": log_loss(y_val, p_val),
        "ece_raw": ece(y_val, p_val),
    }
    if cfg.calibration == "none":
        return None, metrics
    base = float(data.y.iloc[rows.fit].to_numpy(np.float64).mean())
    if len(np.unique(y_val)) < 2:  # nothing to calibrate on, so no skill shown: the base rate
        calibrator = base_rate_map(base)
        p_cal = calibrator.transform(p_val)
        return calibrator, metrics | {
            "log_loss_cal": log_loss(y_val, p_cal),
            "ece_cal": ece(y_val, p_cal),
            "base_rate": base,
            "n_eff": float(_uniqueness(rows.times[val], rows.label_end[val]).sum()),
            "shrinkage": 0.0,
            "fallback": 1.0,
            "log_loss_crossfit": math.nan,
            "log_loss_crossfit_base_rate": math.nan,
        }
    unique = _uniqueness(rows.times[val], rows.label_end[val])
    weight = unique if cfg.sample_weights == "uniqueness" else None
    n_eff = float(unique.sum())
    method: Literal["isotonic", "platt"] = "isotonic"
    if cfg.calibration == "platt" or (
        cfg.calibration == "auto" and len(val) <= cfg.isotonic_min_samples
    ):
        method = "platt"
    calibrator = fit_calibrator(
        p_val, y_val, method, isotonic_min_samples=cfg.isotonic_min_samples, sample_weight=weight
    )
    shrinkage = n_eff / (n_eff + cfg.shrinkage_prior)
    calibrator.shrink(base, shrinkage)
    honest = _crossfit_loss(
        p_val, y_val, unique, weight is not None, rows, method, base, cfg, embargo
    )
    fallback = honest is None or honest[0] >= honest[1]
    if fallback:
        calibrator.shrink(base, 0.0)
    p_cal = calibrator.transform(p_val)
    metrics |= {
        "log_loss_cal": log_loss(y_val, p_cal),
        "ece_cal": ece(y_val, p_cal),
        "base_rate": base,
        "n_eff": n_eff,
        "shrinkage": calibrator.weight,
        "fallback": float(fallback),
        "log_loss_crossfit": math.nan if honest is None else honest[0],
        "log_loss_crossfit_base_rate": math.nan if honest is None else honest[1],
    }
    return calibrator, metrics


def _crossfit_loss(
    p_val: FloatArray,
    y_val: FloatArray,
    unique: FloatArray,
    weighted: bool,
    rows: _Rows,
    method: Literal["isotonic", "platt"],
    base: float,
    cfg: MlPipelineConfig,
    embargo: pd.Timedelta,
) -> tuple[float, float] | None:
    """The calibrated, shrunk map's validation log loss and the base rate's, cross-fitted over a
    purged k-fold with embargo of the validation rows (module docstring); None if no fold can be
    scored."""
    times = rows.times[rows.val]
    ends = pd.Series(
        pd.DatetimeIndex(rows.label_end[rows.val].astype("datetime64[ns]")), index=times
    )
    weight_end = None
    if weighted:
        unique_frame = label_uniqueness(pd.Series(times, index=times), ends.dt.tz_localize("UTC"))
        weight_end = unique_frame["weight_end"]
    splits = PurgedKFold(min(cfg.inner_splits, len(times)), embargo).split(
        times, ends.dt.tz_localize("UTC"), weight_end=weight_end
    )
    pairs = [(split.train_idx, split.test_idx) for split in splits]
    loss_model = loss_base = total = 0.0
    for train, score in pairs:
        if len(np.unique(y_val[train])) < 2:
            continue
        fitted = Calibrator(method).fit(
            p_val[train], y_val[train], unique[train] if weighted else None
        )
        n_eff = float(unique[train].sum())
        fitted.shrink(base, n_eff / (n_eff + cfg.shrinkage_prior))
        w = unique[score] if weighted else np.ones(len(score))
        loss_model += float(w @ _row_log_loss(y_val[score], fitted.transform(p_val[score])))
        loss_base += float(w @ _row_log_loss(y_val[score], np.full(len(score), base)))
        total += float(w.sum())
    if total <= 0:
        return None
    return loss_model / total, loss_base / total


def _uniqueness(times: pd.DatetimeIndex, label_end: npt.NDArray[np.int64]) -> FloatArray:
    """The raw average uniqueness of labels starting at `times` and ending at `label_end`."""
    ends = pd.DatetimeIndex(label_end.astype("datetime64[ns]")).tz_localize("UTC")
    unique = label_uniqueness(pd.Series(times, index=times), pd.Series(ends, index=times))
    values: FloatArray = unique["uniqueness"].to_numpy(np.float64)
    return values


def _row_log_loss(y: FloatArray, p: FloatArray) -> FloatArray:
    clipped = np.clip(p, 1e-15, 1 - 1e-15)
    out: FloatArray = -(y * np.log(clipped) + (1 - y) * np.log(1 - clipped))
    return out


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
