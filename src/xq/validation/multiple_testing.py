"""Multiple-testing control per registered test family (VAL-006).

A *test family* is a set of hypothesis tests whose error rate is controlled together: the tests of
one pre-registered hypothesis's planned analysis, one board's strategies, one study's lags. Each
family declares how:

- **Holm** (1979) controls the family-wise error rate — the probability of any false rejection:
  over the ascending p-values ``p_(1) <= ... <= p_(m)``, ``adj_(i) = max_{j <= i} min(1,
  (m - j + 1) p_(j))``. It is the default: the evidence gates speak of single claims, and a false
  "this strategy works" is the costly error.
- **Benjamini-Hochberg** (1995) controls the false discovery rate — the expected share of false
  rejections among rejections: ``adj_(i) = min_{j >= i} min(1, m p_(j) / j)``. Only for families
  that declare it (screening many candidates for follow-up, never for a gate).
- **Bonferroni**, ``min(1, m p)``, for reference.

Adjusted p-values are returned in the input order. Missing p-values (NaN) stay missing and do not
count towards m. `adjust_by_family` applies each family's method within that family only, so tests
of different families never dilute each other.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

Method = Literal["holm", "bh", "bonferroni"]
FloatArray = npt.NDArray[np.float64]


def holm(p_values: npt.ArrayLike) -> FloatArray:
    """Holm step-down adjusted p-values (module docstring)."""
    p, finite, order = _prepare(p_values)
    adjusted = np.full(p.shape, np.nan)
    m = len(finite)
    running = 0.0
    for rank, position in enumerate(order):
        running = max(running, min(1.0, (m - rank) * float(p[position])))
        adjusted[position] = running
    return adjusted


def benjamini_hochberg(p_values: npt.ArrayLike) -> FloatArray:
    """Benjamini-Hochberg step-up adjusted p-values (module docstring)."""
    p, finite, order = _prepare(p_values)
    adjusted = np.full(p.shape, np.nan)
    m = len(finite)
    running = 1.0
    for rank in range(m - 1, -1, -1):
        position = order[rank]
        running = min(running, min(1.0, m * float(p[position]) / (rank + 1)))
        adjusted[position] = running
    return adjusted


def bonferroni(p_values: npt.ArrayLike) -> FloatArray:
    """Bonferroni adjusted p-values (module docstring)."""
    p, finite, _ = _prepare(p_values)
    adjusted = np.full(p.shape, np.nan)
    adjusted[finite] = np.minimum(1.0, len(finite) * p[finite])
    return adjusted


ADJUSTERS = {"holm": holm, "bh": benjamini_hochberg, "bonferroni": bonferroni}


def adjust(p_values: npt.ArrayLike, method: Method = "holm") -> FloatArray:
    """Adjusted p-values of one family by `method`."""
    try:
        adjuster = ADJUSTERS[method]
    except KeyError:
        raise ValueError(f"unknown method {method!r}; use one of {sorted(ADJUSTERS)}") from None
    return adjuster(p_values)


def adjust_by_family(
    tests: pd.DataFrame,
    *,
    methods: Mapping[str, Method] | None = None,
    default: Method = "holm",
    family: str = "family",
    p_value: str = "p_value",
    level: float = 0.05,
) -> pd.DataFrame:
    """`tests` with ``adjusted_p``, ``method`` and ``rejected``, adjusted within each family.

    Args:
        tests: One row per test, with a family column and a p-value column.
        methods: The method each family declared (families not listed use `default`).
        default: Method of the families not in `methods` (Holm: family-wise error).
        family: Name of the family column.
        p_value: Name of the p-value column.
        level: Rejection level applied to the adjusted p-values.
    """
    missing = [c for c in (family, p_value) if c not in tests.columns]
    if missing:
        raise ValueError(f"tests lack columns {missing}")
    declared = dict(methods or {})
    out = tests.copy()
    out["adjusted_p"] = np.nan
    out["method"] = ""
    for name, rows in out.groupby(family, sort=False):
        method = declared.get(str(name), default)
        out.loc[rows.index, "adjusted_p"] = adjust(rows[p_value].to_numpy(np.float64), method)
        out.loc[rows.index, "method"] = method
    out["rejected"] = out["adjusted_p"] <= level
    return out


def _prepare(
    p_values: npt.ArrayLike,
) -> tuple[FloatArray, npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    p = np.asarray(p_values, dtype=np.float64)
    finite = np.flatnonzero(np.isfinite(p))
    if np.any((p[finite] < 0) | (p[finite] > 1)):
        raise ValueError("p-values must lie in [0, 1]")
    order = finite[np.argsort(p[finite], kind="stable")]
    return p, finite, order
