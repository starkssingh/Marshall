"""The typed result every statistical test returns (Phase 5 interface).

`StatResult` carries what a verdict needs: the test and the series it ran on, the statistic, the
p-value, the lags used, the number of observations, the null and alternative hypotheses, the
assumptions the p-value rests on, the level and the decision (``reject``: p-value below the level),
plus test-specific details (critical values, a break date) and any warnings the library raised
while computing it (``notes``), which are kept rather than hidden.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd


@dataclass(frozen=True)
class StatResult:
    """One statistical test on one series (see the module docstring)."""

    test: str
    series: str
    statistic: float
    p_value: float
    lags: int | None
    nobs: int
    null: str
    alternative: str
    alpha: float
    assumptions: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def reject(self) -> bool:
        """True if the p-value is below the level (never for an undefined p-value)."""
        return math.isfinite(self.p_value) and self.p_value < self.alpha

    def as_row(self) -> dict[str, Any]:
        """A flat table row (details prefixed ``detail_``)."""
        row: dict[str, Any] = {
            "test": self.test,
            "series": self.series,
            "statistic": self.statistic,
            "p_value": self.p_value,
            "lags": self.lags,
            "nobs": self.nobs,
            "alpha": self.alpha,
            "reject": self.reject,
            "null": self.null,
        }
        for key, value in self.details.items():
            row[f"detail_{key}"] = value
        row["notes"] = "; ".join(self.notes)
        return row


def results_table(results: Sequence[StatResult]) -> pd.DataFrame:
    """One row per result (`StatResult.as_row`)."""
    return pd.DataFrame([r.as_row() for r in results])


def holm_adjust(p_values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Holm step-down adjusted p-values (family-wise error control), in the input order.

    ``p_(i) -> max_{j <= i} min(1, (m - j + 1) p_(j))`` over the ascending p-values; NaN p-values
    stay NaN and do not count towards m.
    """
    p = np.asarray(p_values, dtype=np.float64)
    adjusted = np.full(p.shape, np.nan)
    finite = np.flatnonzero(np.isfinite(p))
    m = len(finite)
    if m == 0:
        return adjusted
    order = finite[np.argsort(p[finite], kind="stable")]
    running = 0.0
    for rank, position in enumerate(order):
        running = max(running, min(1.0, (m - rank) * float(p[position])))
        adjusted[position] = running
    return adjusted


@contextmanager
def captured_warnings(notes: list[str]) -> Iterator[None]:
    """Collect warnings raised inside the block into `notes` instead of raising or hiding them."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield
    for warning in caught:
        text = f"{warning.category.__name__}: {warning.message}"
        if text not in notes:
            notes.append(text)


def run_captured[T](fn: Callable[[], T], notes: list[str]) -> T:
    """Call `fn` with its warnings collected into `notes`."""
    with captured_warnings(notes):
        return fn()
