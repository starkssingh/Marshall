"""Out-of-sample prediction store (WF-003).

Walk-forward predictions are written as Parquet under
``data/predictions/<experiment_id>/<run_id>/<evaluation>.parquet`` with the columns
`STORE_COLUMNS`: ``decision_time``, ``fold_id``, ``model_version``, ``feature_set_version``,
``y_true``, ``y_pred``, ``p_raw``, ``p_cal`` and ``train_end``.

The writer is the last line of defence against leakage: it refuses the whole frame if any row's
decision time is not strictly after its fold's ``train_end + embargo`` (the latest label used for
fitting or selection, ADR 0027), or if decision times are naive, duplicated or unsorted. Nothing is
written in that case. Every file is recorded as a run artifact with its SHA-256.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from xq.core.errors import NaiveTimestampError, XQError

if TYPE_CHECKING:
    from xq.tracking.runs import RunContext

PREDICTIONS_DIR = "predictions"
STORE_COLUMNS = (
    "decision_time",
    "fold_id",
    "model_version",
    "feature_set_version",
    "y_true",
    "y_pred",
    "p_raw",
    "p_cal",
    "train_end",
)
_VALUE_COLUMNS = ("fold_id", "y_true", "y_pred", "p_raw", "p_cal", "train_end")


class PredictionLeakError(XQError):
    """A prediction was made at or before its fold's training information cutoff plus embargo."""


def check_predictions(predictions: pd.DataFrame, embargo: pd.Timedelta) -> None:
    """Refuse predictions that could have seen their own outcome (see the module docstring).

    Args:
        predictions: Indexed by decision time, with at least ``fold_id``, ``y_true``, ``y_pred``,
            ``p_raw``, ``p_cal`` and ``train_end``.
        embargo: The splitter's embargo.

    Raises:
        PredictionLeakError: naming the first offending row and fold.
        NaiveTimestampError: for naive decision times or ``train_end``.
        ValueError: for missing columns, duplicated or unsorted decision times.
    """
    missing = [c for c in _VALUE_COLUMNS if c not in predictions.columns]
    if missing:
        raise ValueError(f"predictions lack columns {missing}")
    index = predictions.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise NaiveTimestampError("predictions must be indexed by tz-aware decision times")
    if not index.is_unique or not index.is_monotonic_increasing:
        raise ValueError("decision times must be unique and increasing")
    train_end = pd.DatetimeIndex(predictions["train_end"])
    if train_end.tz is None:
        raise NaiveTimestampError("train_end must be tz-aware")
    ok = np.asarray(index > train_end + embargo) & ~np.asarray(train_end.isna())
    if not ok.all():
        first = int(np.argmin(ok))
        raise PredictionLeakError(
            f"prediction at {index[first]} (fold {predictions['fold_id'].iloc[first]}) is not "
            f"after train_end {train_end[first]} + embargo {embargo}; "
            f"{int((~ok).sum())} leaked row(s), nothing written"
        )


def write_predictions(
    run: RunContext,
    evaluation: str,
    predictions: pd.DataFrame,
    *,
    embargo: pd.Timedelta,
    model_version: str,
    feature_set_version: str,
) -> Path:
    """Check and store one evaluation's predictions for `run`; return the file path.

    Raises:
        PredictionLeakError, NaiveTimestampError, ValueError: see `check_predictions`.
    """
    check_predictions(predictions, embargo)
    frame = predictions.loc[:, list(_VALUE_COLUMNS)].copy()
    frame.insert(1, "model_version", model_version)
    frame.insert(2, "feature_set_version", feature_set_version)
    table = frame.reset_index(names="decision_time").loc[:, list(STORE_COLUMNS)]
    root = run.cfg.paths.resolve(run.cfg.paths.data_dir) / PREDICTIONS_DIR
    directory = root / run.run.experiment_id / run.run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_slug(evaluation)}.parquet"
    staging = path.with_name(f".{path.name}.tmp")
    table.to_parquet(staging, index=False)
    staging.replace(path)
    run.log_artifact(path, kind="predictions")
    return path


def read_predictions(path: Path) -> pd.DataFrame:
    """A stored prediction file, indexed by tz-aware decision time."""
    frame = pd.read_parquet(path)
    return frame.set_index(pd.DatetimeIndex(frame.pop("decision_time"), name="decision_time"))


def _slug(evaluation: str) -> str:
    """A file-name-safe name that stays unique: readable part plus a short hash."""
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", evaluation).strip("_") or "evaluation"
    digest = hashlib.sha256(evaluation.encode("utf-8")).hexdigest()[:8]
    return f"{readable[:80]}-{digest}"
