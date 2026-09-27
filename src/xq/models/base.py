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
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import product
from typing import Any, Literal, Protocol

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
