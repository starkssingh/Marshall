"""Linear forecaster families (ML-001; Stage A, ML-004, uses them).

- ``logistic``: scikit-learn's `LogisticRegression` (classification) with an elastic-net
  penalty: ``C`` (inverse strength) and ``l1_ratio`` (0 = L2, 1 = L1), the ``saga`` solver,
  ``max_iter`` iterations, seeded;
- ``ridge``: scikit-learn's `Ridge` (regression) with penalty ``alpha``.

Inputs are expected standardized by a training-fold scaler (ML-002); the models never size
positions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sklearn.linear_model import LogisticRegression, Ridge

from xq.models.base import ForecasterSpec


def _logistic(params: Mapping[str, Any], seed: int) -> LogisticRegression:
    return LogisticRegression(
        C=float(params.get("C", 1.0)),
        l1_ratio=float(params.get("l1_ratio", 0.0)),
        solver="saga",
        max_iter=int(params.get("max_iter", 5000)),
        random_state=seed,
    )


def _ridge(params: Mapping[str, Any], seed: int) -> Ridge:
    return Ridge(alpha=float(params.get("alpha", 1.0)), random_state=seed)


LOGISTIC = ForecasterSpec("logistic", 1, "classification", _logistic)
RIDGE = ForecasterSpec("ridge", 1, "regression", _ridge)
LINEAR: tuple[ForecasterSpec, ...] = (LOGISTIC, RIDGE)
