"""Tree forecaster families (ML-001; Stage B, ML-005, uses them).

- ``random_forest``: scikit-learn's `RandomForestClassifier`, conservative by default:
  ``n_estimators`` trees, ``max_depth``, ``min_samples_leaf`` and ``max_samples`` (the share of
  rows each tree sees, reduced because labels overlap), seeded.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sklearn.ensemble import RandomForestClassifier

from xq.models.base import ForecasterSpec


def _random_forest(params: Mapping[str, Any], seed: int) -> RandomForestClassifier:
    depth = params.get("max_depth", 4)
    return RandomForestClassifier(
        n_estimators=int(params.get("n_estimators", 200)),
        max_depth=None if depth is None else int(depth),
        min_samples_leaf=int(params.get("min_samples_leaf", 50)),
        max_samples=float(params.get("max_samples", 0.5)),
        random_state=seed,
        n_jobs=1,
    )


RANDOM_FOREST = ForecasterSpec("random_forest", 1, "classification", _random_forest)
TREES: tuple[ForecasterSpec, ...] = (RANDOM_FOREST,)
