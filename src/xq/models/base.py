"""The estimator interface used by the walk-forward runner (WF-002).

A model family is a `ModelSpec`: a name, a code version (part of every cache key and trial
configuration — bump it whenever the model's behaviour changes), the task it solves and a factory
that builds a fresh `Estimator` from parameters. Estimators are fitted on one fold's training rows
only, with a generator seeded per fold, and predict one number per row:

- ``regression``: the forecast of the target value;
- ``classification``: the probability that the target is positive (``y = 1`` when the target
  value is above zero).

Models never size positions (CLAUDE.md): they forecast, and strategies and the risk engine decide.

A `ModelConfig` (from an experiment config) names the family, its fixed parameters, an optional
grid of candidates selected on the validation window, and the feature columns it reads.

**The `Forecaster` protocol (ML-001)** is the interface of the machine-learning research
(Phase 11): ``fit(x, y, sample_weight=, eval_set=)``, ``predict`` and ``predict_proba``,
``save`` and ``load``, and ``model_card()``. `SklearnForecaster` wraps a scikit-learn estimator
of a registered family (`forecaster_spec`: ``logistic`` and ``ridge`` in `xq.models.linear`,
``random_forest`` in `xq.models.trees`): it is seeded, refuses missing inputs, remembers its
feature columns and refuses another column order at prediction, and returns P(y = 1) as a float
array. ``eval_set`` is accepted by every forecaster and used only by a family that stops early on
it (none yet); a pipeline passes inner validation rows only (ML-002), never test rows.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Literal, Protocol

import joblib
import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

Task = Literal["regression", "classification"]


class Estimator(Protocol):
    """A model instance for one fold."""

    def fit(self, x: pd.DataFrame, y: pd.Series, rng: np.random.Generator) -> None:
        """Fit on training rows (`y` has no missing values)."""

    def predict(self, x: pd.DataFrame) -> npt.NDArray[np.float64]:
        """One forecast per row of `x` (a probability of ``y = 1`` for classification)."""


@dataclass(frozen=True)
class ModelSpec:
    """A model family: how to build an estimator and what it predicts."""

    name: str
    code_version: int
    task: Task
    factory: Callable[[Mapping[str, Any]], Estimator]


class ModelConfig(BaseModel):
    """Which model to run, with which parameters, on which features."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    params: dict[str, Any] = {}
    grid: dict[str, list[Any]] = Field(default_factory=dict)
    features: list[str] = []

    def candidates(self) -> list[dict[str, Any]]:
        """Fixed parameters combined with every grid point (keys in sorted order)."""
        keys = sorted(self.grid)
        return [
            {**self.params, **dict(zip(keys, values, strict=True))}
            for values in product(*(self.grid[k] for k in keys))
        ]

    def config_hash(self, spec: ModelSpec) -> str:
        """16-hex hash of this configuration and the model's code version and task."""
        payload = {
            "config": self.model_dump(mode="json"),
            "code_version": spec.code_version,
            "task": spec.task,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def model_targets(values: pd.Series, task: Task) -> pd.Series:
    """What a model of `task` learns from target values: the value, or ``1.0`` if it is positive.

    Missing values stay missing.
    """
    if task == "regression":
        return values.astype(np.float64)
    classes = (values > 0).astype(np.float64)
    return classes.where(values.notna())


# --- ML-001: the Forecaster protocol and scikit-learn wrappers --------------------------------


class Forecaster(Protocol):
    """A model of the machine-learning research (module docstring, ML-001)."""

    name: str
    task: Task

    def fit(
        self,
        x: pd.DataFrame,
        y: pd.Series,
        *,
        sample_weight: pd.Series | None = None,
        eval_set: tuple[pd.DataFrame, pd.Series] | None = None,
    ) -> Forecaster:
        """Fit on training rows (no missing values) and return self."""

    def predict(self, x: pd.DataFrame) -> npt.NDArray[np.float64]:
        """The forecast (regression) or the predicted class, 0.0 or 1.0 (classification)."""

    def predict_proba(self, x: pd.DataFrame) -> npt.NDArray[np.float64]:
        """P(y = 1) per row (classification only)."""

    def save(self, path: Path) -> None:
        """Write the fitted model to `path`."""

    def model_card(self) -> dict[str, Any]:
        """What the model is: family, code version, task, parameters, seed, features."""


class ForecasterError(ValueError):
    """A forecaster used before fitting, on missing inputs or on other columns."""


@dataclass(frozen=True)
class ForecasterSpec:
    """A forecaster family: its code version, task and how to build its scikit-learn estimator
    from parameters and a seed."""

    name: str
    code_version: int
    task: Task
    build: Callable[[Mapping[str, Any], int], Any]
    early_stopping: bool = False


class SklearnForecaster:
    """A `Forecaster` wrapping one scikit-learn estimator (module docstring)."""

    def __init__(self, spec: ForecasterSpec, params: Mapping[str, Any], seed: int) -> None:
        self.spec = spec
        self.name = spec.name
        self.task: Task = spec.task
        self.params = dict(params)
        self.seed = int(seed)
        self.features: list[str] | None = None
        self.estimator: Any = None

    def fit(
        self,
        x: pd.DataFrame,
        y: pd.Series,
        *,
        sample_weight: pd.Series | None = None,
        eval_set: tuple[pd.DataFrame, pd.Series] | None = None,
    ) -> SklearnForecaster:
        """Fit a fresh estimator on `x` and `y` (with optional per-row weights).

        Raises:
            ForecasterError: for missing values, mismatched lengths, or a classification target
                other than 0/1 or with one class only.
        """
        values = _matrix(x)
        target = y.to_numpy(np.float64)
        if len(target) != len(values) or np.isnan(target).any():
            raise ForecasterError("y must have one known value per row of x")
        if self.task == "classification":
            classes = set(np.unique(target))
            if not classes <= {0.0, 1.0} or len(classes) < 2:
                raise ForecasterError("a classifier needs both classes 0 and 1 in y")
        weight = None
        if sample_weight is not None:
            weight = sample_weight.to_numpy(np.float64)
            if len(weight) != len(values) or np.isnan(weight).any() or (weight < 0).any():
                raise ForecasterError("sample weights must be one non-negative value per row")
        estimator = self.spec.build(self.params, self.seed)
        if self.task == "classification":
            estimator.fit(values, target.astype(np.int64), sample_weight=weight)
        else:
            estimator.fit(values, target, sample_weight=weight)
        self.estimator = estimator
        self.features = [str(c) for c in x.columns]
        return self

    def predict_proba(self, x: pd.DataFrame) -> npt.NDArray[np.float64]:
        """P(y = 1) per row.

        Raises:
            ForecasterError: for a regression model, before fitting, or for other columns.
        """
        if self.task != "classification":
            raise ForecasterError(f"{self.name} is a regression model: it has no probabilities")
        estimator = self._fitted(x)
        column = list(estimator.classes_).index(1)
        proba: npt.NDArray[np.float64] = np.asarray(
            estimator.predict_proba(_matrix(x))[:, column], dtype=np.float64
        )
        return proba

    def predict(self, x: pd.DataFrame) -> npt.NDArray[np.float64]:
        """The forecast, or the class (P(y = 1) above one half) for a classifier."""
        if self.task == "classification":
            return (self.predict_proba(x) > 0.5).astype(np.float64)
        out: npt.NDArray[np.float64] = np.asarray(
            self._fitted(x).predict(_matrix(x)), dtype=np.float64
        )
        return out

    def save(self, path: Path) -> None:
        """Write the fitted model to `path` (a joblib file of this object).

        Raises:
            ForecasterError: before fitting.
        """
        self._fitted(None)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @classmethod
    def load(cls, path: Path) -> SklearnForecaster:
        """A forecaster written by `save` (only files this project wrote: joblib unpickles).

        Raises:
            ForecasterError: if the file does not hold a `SklearnForecaster`.
        """
        loaded = joblib.load(path)
        if not isinstance(loaded, cls):
            raise ForecasterError(f"{path} does not hold a {cls.__name__}")
        return loaded

    def model_card(self) -> dict[str, Any]:
        """Family, code version, task, parameters, seed and feature columns."""
        return {
            "family": self.name,
            "code_version": self.spec.code_version,
            "task": self.task,
            "params": dict(sorted(self.params.items())),
            "seed": self.seed,
            "features": list(self.features or []),
        }

    def _fitted(self, x: pd.DataFrame | None) -> Any:
        if self.estimator is None or self.features is None:
            raise ForecasterError(f"{self.name} is not fitted")
        if x is not None and [str(c) for c in x.columns] != self.features:
            raise ForecasterError(
                f"{self.name} was fitted on columns {self.features}, not {list(x.columns)}"
            )
        return self.estimator


def forecaster_spec(name: str) -> ForecasterSpec:
    """The registered forecaster family `name`.

    Raises:
        KeyError: for an unknown family.
    """
    from xq.models.linear import LINEAR
    from xq.models.trees import TREES

    families = {spec.name: spec for spec in (*LINEAR, *TREES)}
    if name not in families:
        raise KeyError(f"unknown forecaster {name!r}; registered: {sorted(families)}")
    return families[name]


def build_forecaster(name: str, params: Mapping[str, Any], seed: int) -> SklearnForecaster:
    """An unfitted forecaster of family `name`."""
    return SklearnForecaster(forecaster_spec(name), params, seed)


def _matrix(x: pd.DataFrame) -> npt.NDArray[np.float64]:
    values = x.to_numpy(np.float64)
    if np.isnan(values).any() or np.isinf(values).any():
        raise ForecasterError("inputs must not be missing or infinite (drop such rows first)")
    return values
