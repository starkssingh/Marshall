"""Small estimators for testing the walk-forward runner.

They live in an importable module (not a test file) so worker processes started with ``spawn``
can unpickle them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.models.base import ModelSpec


class ShrunkMean:
    """Predicts ``shrink * training mean`` (``shrink`` defaults to 1)."""

    def __init__(self, params: Mapping[str, Any]) -> None:
        self.shrink = float(params.get("shrink", 1.0))
        self.value = 0.0

    def fit(self, x: pd.DataFrame, y: pd.Series, rng: np.random.Generator) -> None:
        self.value = self.shrink * float(y.mean())

    def predict(self, x: pd.DataFrame) -> npt.NDArray[np.float64]:
        return np.full(len(x), self.value)


class NoisyMean(ShrunkMean):
    """The training mean plus noise from the fold's generator (tests seeding across processes)."""

    def fit(self, x: pd.DataFrame, y: pd.Series, rng: np.random.Generator) -> None:
        super().fit(x, y, rng)
        self.rng = rng

    def predict(self, x: pd.DataFrame) -> npt.NDArray[np.float64]:
        return self.value + self.rng.normal(0, 1, size=len(x))


class LeastSquares:
    """Ordinary least squares with an intercept on every feature column."""

    def __init__(self, params: Mapping[str, Any]) -> None:
        self.coef = np.zeros(1)

    def fit(self, x: pd.DataFrame, y: pd.Series, rng: np.random.Generator) -> None:
        design = np.column_stack([np.ones(len(x)), x.to_numpy(np.float64)])
        self.coef = np.linalg.lstsq(design, y.to_numpy(np.float64), rcond=None)[0]

    def predict(self, x: pd.DataFrame) -> npt.NDArray[np.float64]:
        design = np.column_stack([np.ones(len(x)), x.to_numpy(np.float64)])
        forecast: npt.NDArray[np.float64] = design @ self.coef
        return forecast


class LastLabel:
    """Predicts the target of the latest training row: memorizes, so it exposes label overlap."""

    def __init__(self, params: Mapping[str, Any]) -> None:
        self.value = 0.0

    def fit(self, x: pd.DataFrame, y: pd.Series, rng: np.random.Generator) -> None:
        self.value = float(y.iloc[-1])

    def predict(self, x: pd.DataFrame) -> npt.NDArray[np.float64]:
        return np.full(len(x), self.value)


def shrunk_mean(params: Mapping[str, Any]) -> ShrunkMean:
    return ShrunkMean(params)


def noisy_mean(params: Mapping[str, Any]) -> NoisyMean:
    return NoisyMean(params)


def least_squares(params: Mapping[str, Any]) -> LeastSquares:
    return LeastSquares(params)


def last_label(params: Mapping[str, Any]) -> LastLabel:
    return LastLabel(params)


MEAN = ModelSpec("mean", 1, "regression", shrunk_mean)
NOISY = ModelSpec("noisy_mean", 1, "regression", noisy_mean)
OLS = ModelSpec("ols", 1, "regression", least_squares)
LAST = ModelSpec("last_label", 1, "regression", last_label)
FREQUENCY = ModelSpec("frequency", 1, "classification", shrunk_mean)
