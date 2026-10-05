"""Probability calibration fitted on validation rows only (ML-002).

A classifier's raw probabilities are recalibrated by a map fitted on the fold's **validation**
rows (the last part of its training window, after purging), never on test rows:

- **isotonic** regression (a non-decreasing step function, clipped to the range it saw) when there
  are more than ``isotonic_min_samples`` validation rows (the plan: 1,000);
- **Platt** scaling otherwise: a logistic regression of the outcome on ``logit(p_raw)`` with a
  unit L2 penalty on the slope (`PLATT_C`; the intercept is not penalized).

Both accept per-row weights. The pipeline passes the validation labels' **raw average
uniqueness** (TGT-006): overlapping labels then count as the few independent observations they
are, so a slope fitted on a handful of independent outcomes is shrunk towards the base rate
instead of extrapolating a spurious validation pattern to the test rows.

``auto`` chooses between them by that count; ``none`` leaves the probabilities as they are.
Calibrated probabilities are clipped to ``[1e-6, 1 - 1e-6]`` so log loss stays finite.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import numpy.typing as npt
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

FloatArray = npt.NDArray[np.float64]
Method = Literal["auto", "isotonic", "platt", "none"]
EPS = 1e-6
#: Inverse L2 strength of Platt scaling's slope (scikit-learn's default).
PLATT_C = 1.0


class CalibrationError(ValueError):
    """A calibrator is fitted on unusable rows or used before fitting."""


class Calibrator:
    """A fitted probability map (module docstring)."""

    def __init__(self, method: Literal["isotonic", "platt", "none"]) -> None:
        self.method = method
        self._isotonic: IsotonicRegression | None = None
        self._platt: LogisticRegression | None = None
        self.fitted = False

    def fit(
        self, p_raw: npt.ArrayLike, y: npt.ArrayLike, sample_weight: npt.ArrayLike | None = None
    ) -> Calibrator:
        """Fit on validation probabilities and outcomes (0/1), optionally weighted per row.

        Raises:
            CalibrationError: for mismatched lengths, missing values, a single class or negative
                weights.
        """
        p, outcome = _checked(p_raw, y)
        weight = None
        if sample_weight is not None:
            weight = np.asarray(sample_weight, dtype=np.float64)
            if len(weight) != len(p) or np.isnan(weight).any() or (weight < 0).any():
                raise CalibrationError("calibration weights must be one non-negative value per row")
        if self.method == "isotonic":
            model = IsotonicRegression(y_min=EPS, y_max=1 - EPS, out_of_bounds="clip")
            model.fit(p, outcome, sample_weight=weight)
            self._isotonic = model
        elif self.method == "platt":
            platt = LogisticRegression(C=PLATT_C, solver="lbfgs", max_iter=1000)
            platt.fit(_logit(p).reshape(-1, 1), outcome.astype(np.int64), sample_weight=weight)
            self._platt = platt
        self.fitted = True
        return self

    def transform(self, p_raw: npt.ArrayLike) -> FloatArray:
        """Calibrated probabilities (NaN stays NaN).

        Raises:
            CalibrationError: before fitting.
        """
        if not self.fitted:
            raise CalibrationError("fit the calibrator on validation rows first")
        p = np.asarray(p_raw, dtype=np.float64)
        out = np.full(len(p), np.nan)
        known = ~np.isnan(p)
        if self.method == "isotonic" and self._isotonic is not None:
            out[known] = self._isotonic.predict(p[known])
        elif self.method == "platt" and self._platt is not None:
            out[known] = self._platt.predict_proba(_logit(p[known]).reshape(-1, 1))[:, 1]
        else:
            out[known] = p[known]
        clipped: FloatArray = np.where(known, np.clip(out, EPS, 1 - EPS), np.nan)
        return clipped


def fit_calibrator(
    p_raw: npt.ArrayLike,
    y: npt.ArrayLike,
    method: Method = "auto",
    *,
    isotonic_min_samples: int,
    sample_weight: npt.ArrayLike | None = None,
) -> Calibrator:
    """A calibrator of `method` fitted on validation rows (optionally weighted); ``auto`` is
    isotonic above `isotonic_min_samples` rows and Platt otherwise."""
    n = len(np.asarray(p_raw))
    automatic: Literal["isotonic", "platt"] = "isotonic" if n > isotonic_min_samples else "platt"
    chosen: Literal["isotonic", "platt", "none"] = automatic if method == "auto" else method
    return Calibrator(chosen).fit(p_raw, y, sample_weight)


def _checked(p_raw: npt.ArrayLike, y: npt.ArrayLike) -> tuple[FloatArray, FloatArray]:
    p = np.asarray(p_raw, dtype=np.float64)
    outcome = np.asarray(y, dtype=np.float64)
    if len(p) != len(outcome) or np.isnan(p).any() or np.isnan(outcome).any():
        raise CalibrationError("calibration needs one known probability per known outcome")
    if not set(np.unique(outcome)) <= {0.0, 1.0} or len(np.unique(outcome)) < 2:
        raise CalibrationError("calibration needs outcomes of both classes 0 and 1")
    return p, outcome


def _logit(p: FloatArray) -> FloatArray:
    clipped = np.clip(p, EPS, 1 - EPS)
    out: FloatArray = np.log(clipped / (1 - clipped))
    return out
