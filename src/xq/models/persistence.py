"""Model persistence and model cards (ML-009).

`save_trained_fold(directory, trained, data, dataset_id=, feature_set=)` writes one fold's model:

- ``model.joblib`` — the fitted forecaster with its training-fold scaler and its calibrator (what
  turns inputs into ``p_raw`` and ``p_cal``);
- ``card.json`` — the **model card**: the family, its code version and task, the hyperparameters,
  the seed, the input columns, the feature-set version and dataset id, the fold id and its
  training cutoff, the fitting and validation row counts and their validation metrics, the
  calibration's base rate, shrinkage and no-skill fallback (C-34 (1)), the rows and columns
  left out (C-34 (2)), a SHA-256
  of the training data (the fitting rows' inputs, targets and decision times), the library
  versions (Python, NumPy, pandas, scikit-learn, joblib, Optuna) and the artifact's SHA-256.

`load_model(directory)` reads it back. It refuses an artifact whose bytes no longer match the
card's hash, and, unless asked otherwise, one written by other library versions (unpickling across
versions can change behaviour silently). The loaded `PersistedModel` predicts exactly as the
pipeline did: scale with the stored scaler, ``p_raw`` from the forecaster, ``p_cal`` from the
calibrator. **A reload reproduces the pipeline's test predictions within 1e-9** (tested).

joblib unpickles: load only artifacts this project wrote (the hash check guards their integrity,
not their origin).
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import BaseModel, ConfigDict

from xq.core.errors import XQError
from xq.features.base import TrainingFoldScaler
from xq.models.base import SklearnForecaster
from xq.models.calibration import Calibrator
from xq.models.pipeline import FoldData, TrainedFold, scaled_inputs

ARTIFACT = "model.joblib"
CARD = "card.json"
#: Libraries whose versions a model card records and a load compares.
LIBRARIES = ("numpy", "pandas", "scikit-learn", "joblib", "optuna")


class PersistenceError(XQError):
    """A stored model whose artifact or library versions do not match its card."""


class ModelCard(BaseModel):
    """What a stored model is and how it was made (module docstring)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: str
    code_version: int
    task: str
    params: dict[str, Any]
    seed: int
    features: list[str]
    feature_set: str
    dataset_id: str
    fold_id: str
    train_end: str
    n_fit: int
    n_val: int
    #: Validation metrics; a metric that could not be computed (NaN) is None.
    val_metrics: dict[str, float | None]
    calibration: str
    #: C-34 (1): the training base rate, the share of the calibrated distance from it kept, and
    #: whether the fold predicts the base rate (no skill shown on validation).
    base_rate: float | None = None
    shrinkage: float | None = None
    fallback: bool = False
    #: C-34 (2): rows and columns left out of the fold (``TrainedFold.dropped``).
    dropped: dict[str, int] = {}
    training_data_sha256: str
    library_versions: dict[str, str]
    artifact_sha256: str


@dataclass(frozen=True)
class PersistedModel:
    """A stored fold model: its forecaster, scaler, calibrator and card."""

    forecaster: SklearnForecaster
    scaler: TrainingFoldScaler
    calibrator: Calibrator | None
    card: ModelCard

    def predict(self, x: pd.DataFrame) -> pd.DataFrame:
        """``p_raw`` and ``p_cal`` (classification) or ``y_pred`` (regression) per row of `x`,
        computed as the pipeline computed them."""
        scaled = scaled_inputs(self.scaler, x.loc[:, self.card.features])
        if self.forecaster.task != "classification":
            return pd.DataFrame({"y_pred": self.forecaster.predict(scaled)}, index=x.index)
        p = self.forecaster.predict_proba(scaled)
        cal = self.calibrator.transform(p) if self.calibrator is not None else p
        return pd.DataFrame({"p_raw": p, "p_cal": cal}, index=x.index)


def library_versions() -> dict[str, str]:
    """The Python and library versions a card records."""
    return {"python": platform.python_version(), **{lib: version(lib) for lib in LIBRARIES}}


def training_data_hash(data: FoldData, rows: pd.DatetimeIndex) -> str:
    """SHA-256 of the fitting rows' inputs, targets and decision times."""
    digest = hashlib.sha256()
    for part in (data.x.loc[rows], data.y.loc[rows].to_frame()):
        digest.update(pd.util.hash_pandas_object(part, index=True).to_numpy().tobytes())
        digest.update(",".join(map(str, part.columns)).encode("utf-8"))
    return digest.hexdigest()


def save_trained_fold(
    directory: Path, trained: TrainedFold, data: FoldData, *, dataset_id: str, feature_set: str
) -> ModelCard:
    """Write `trained` and its model card to `directory` (module docstring)."""
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / ARTIFACT
    joblib.dump(
        {
            "forecaster": trained.forecaster,
            "scaler": trained.scaler,
            "calibrator": trained.calibrator,
        },
        artifact,
    )
    forecaster_card = trained.forecaster.model_card()
    card = ModelCard(
        family=forecaster_card["family"],
        code_version=forecaster_card["code_version"],
        task=forecaster_card["task"],
        params=forecaster_card["params"],
        seed=forecaster_card["seed"],
        features=forecaster_card["features"],
        feature_set=feature_set,
        dataset_id=dataset_id,
        fold_id=trained.fold_id,
        train_end=str(trained.train_end),
        n_fit=trained.n_fit,
        n_val=trained.n_val,
        val_metrics={
            k: float(v) if math.isfinite(v) else None for k, v in trained.val_metrics.items()
        },
        calibration=trained.calibrator.method if trained.calibrator is not None else "none",
        base_rate=trained.base_rate,
        shrinkage=trained.shrinkage,
        fallback=trained.fallback,
        dropped=dict(trained.dropped),
        training_data_sha256=training_data_hash(data, trained.fit_index),
        library_versions=library_versions(),
        artifact_sha256=_sha256(artifact),
    )
    (directory / CARD).write_text(card.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return card


def load_model(directory: Path, *, allow_version_drift: bool = False) -> PersistedModel:
    """Read a model written by `save_trained_fold`.

    Args:
        allow_version_drift: Load even if the library versions differ from the card's.

    Raises:
        PersistenceError: if the artifact's hash differs from the card's, or the library versions
            differ and `allow_version_drift` is False.
    """
    card = ModelCard.model_validate(json.loads((directory / CARD).read_text(encoding="utf-8")))
    artifact = directory / ARTIFACT
    if _sha256(artifact) != card.artifact_sha256:
        raise PersistenceError(f"{artifact} does not match its model card's SHA-256")
    current = library_versions()
    drift = {
        k: (v, current.get(k)) for k, v in card.library_versions.items() if current.get(k) != v
    }
    if drift and not allow_version_drift:
        raise PersistenceError(f"the model was written with other library versions: {drift}")
    parts = joblib.load(artifact)
    return PersistedModel(parts["forecaster"], parts["scaler"], parts["calibrator"], card)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def max_abs_difference(a: npt.ArrayLike, b: npt.ArrayLike) -> float:
    """The largest absolute difference between two prediction arrays (NaN where both are NaN
    counts as equal)."""
    x, y = np.asarray(a, np.float64), np.asarray(b, np.float64)
    both = np.isnan(x) & np.isnan(y)
    if (np.isnan(x) != np.isnan(y)).any():
        return float("inf")
    return float(np.max(np.abs(x[~both] - y[~both]), initial=0.0))
