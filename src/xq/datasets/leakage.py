"""Leakage harness for features and targets (DS-006).

Features are functions of time-stamped inputs (bars, joined context) that return one row per
decision time. They must use nothing that was not available at that time. The harness checks
this four ways:

a. **Truncation invariance** — computing on inputs cut at a random decision time t (only rows with
   ``available_at <= t``) gives the same values at and before t as computing on the full data.
b. **Future perturbation** — randomizing every input row that becomes available after t leaves the
   values at and before t unchanged.
c. **Availability audit** — every provenance column (a name ending in ``available_at``, as written
   by `asof_join`) is at or before its row's decision time.
d. **Suspicious-correlation scan** — a feature whose absolute correlation with any target at lag 0
   exceeds 0.9 fails pending review (reviewed pairs can be allowed explicitly).

Targets look forward by definition, so they are checked against their own declared bounds: the
value at t must not change when quotes after ``label_end`` are removed, when quotes before t are
perturbed, or when the volatility scale at any time other than t is perturbed (the only
information at or before t a target may use is sigma-hat at t). Structurally, ``label_start`` may
not precede t and ``label_end`` may not precede ``label_start``.

A violation is a leak until proven otherwise; the harness reports the first instant and column
where each check failed.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.seeds import make_rng

Inputs = Mapping[str, pd.DataFrame]
FeatureFn = Callable[[Inputs], pd.DataFrame]
TargetFn = Callable[[pd.DataFrame, pd.Series], pd.DataFrame]
Check = Literal[
    "truncation",
    "perturbation",
    "availability",
    "correlation",
    "label_bounds",
    "after_label_end",
    "before_decision",
    "sigma_other_times",
]

DEFAULT_AVAILABLE_AT = "available_at_utc"
PROVENANCE_SUFFIX = "available_at"
CORRELATION_THRESHOLD = 0.9
_RTOL = 1e-9
_ATOL = 1e-12


@dataclass(frozen=True)
class Violation:
    """The first place a check found information from the future."""

    check: Check
    column: str
    at: pd.Timestamp | None
    detail: str

    def __str__(self) -> str:
        where = f" at {self.at}" if self.at is not None else ""
        return f"[{self.check}] {self.column}{where}: {self.detail}"


@dataclass
class LeakageReport:
    """Outcome of one harness run. `passed` is True only when no check found anything."""

    violations: list[Violation] = field(default_factory=list)
    points: list[pd.Timestamp] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def checks_failed(self) -> set[Check]:
        return {v.check for v in self.violations}

    def summary(self) -> str:
        if self.passed:
            return f"no leakage found at {len(self.points)} test points"
        return "\n".join(str(v) for v in self.violations)

    def _add(self, violation: Violation) -> None:
        key = (violation.check, violation.column)
        for i, existing in enumerate(self.violations):
            if (existing.check, existing.column) == key:
                if violation.at is not None and (existing.at is None or violation.at < existing.at):
                    self.violations[i] = violation
                return
        self.violations.append(violation)


def check_feature_causality(
    fn: FeatureFn,
    inputs: Inputs,
    *,
    available_at: str = DEFAULT_AVAILABLE_AT,
    n_points: int = 25,
    seed: int = 0,
) -> LeakageReport:
    """Run truncation, perturbation and availability checks on a feature function.

    Args:
        fn: Maps inputs to a frame indexed by tz-aware, increasing, unique decision times.
        inputs: Named input frames; each has the availability column `available_at`.
        available_at: Name of the availability column in every input.
        n_points: Number of decision times to cut at (always including the first and last).
        seed: Seed for choosing points and perturbations (via `xq.core.seeds`).
    """
    for name, frame in inputs.items():
        if available_at not in frame.columns:
            raise ValueError(f"input {name!r} has no availability column {available_at!r}")
    full = _checked_output(fn(inputs), "fn(inputs)")
    rng = make_rng(seed)
    report = LeakageReport(points=_points(pd.DatetimeIndex(full.index), n_points, rng))

    _audit_provenance(full, report)
    for t in report.points:
        truncated = {name: frame[frame[available_at] <= t] for name, frame in inputs.items()}
        _compare(full, _checked_output(fn(truncated), "truncated"), t, "truncation", report)
        perturbed = {
            name: _perturb(frame, frame[available_at] > t, rng, keep=available_at)
            for name, frame in inputs.items()
        }
        _compare(full, _checked_output(fn(perturbed), "perturbed"), t, "perturbation", report)
    return report


def correlation_scan(
    features: pd.DataFrame,
    targets: pd.DataFrame,
    *,
    threshold: float = CORRELATION_THRESHOLD,
    min_observations: int = 30,
    allowed: Collection[tuple[str, str]] = (),
) -> LeakageReport:
    """Flag feature/target pairs whose absolute lag-0 correlation exceeds `threshold`.

    Both frames are aligned on their index (decision time). `allowed` lists reviewed
    ``(feature, target)`` pairs that may exceed the threshold.
    """
    report = LeakageReport()
    aligned_features, aligned_targets = features.align(targets, join="inner", axis=0)
    for feature in _numeric_columns(aligned_features):
        x = aligned_features[feature].to_numpy(dtype=np.float64)
        for target in _numeric_columns(aligned_targets):
            y = aligned_targets[target].to_numpy(dtype=np.float64)
            both = np.isfinite(x) & np.isfinite(y)
            if both.sum() < min_observations or (feature, target) in allowed:
                continue
            xs, ys = x[both], y[both]
            if np.std(xs) == 0 or np.std(ys) == 0:
                continue
            corr = float(np.corrcoef(xs, ys)[0, 1])
            if abs(corr) > threshold:
                report._add(
                    Violation(
                        "correlation",
                        feature,
                        None,
                        f"|corr| with target {target!r} is {abs(corr):.3f} > {threshold} "
                        "(fails pending review)",
                    )
                )
    return report


def check_target_bounds(
    fn: TargetFn,
    quotes: pd.DataFrame,
    sigma: pd.Series,
    *,
    time_column: str = "ts_utc",
    n_points: int = 25,
    seed: int = 0,
) -> LeakageReport:
    """Check that a target at t uses only quotes after t up to ``label_end`` and sigma-hat at t.

    Args:
        fn: ``fn(quotes, sigma)`` returning ``value``, ``label_start`` and ``label_end`` indexed
            by decision time (the index of `sigma`).
        quotes: Tick quotes with a tz-aware `time_column`, sorted by it.
        sigma: Volatility scale indexed by decision time.
    """
    full = _checked_output(fn(quotes, sigma), "fn(quotes, sigma)")
    for column in ("value", "label_start", "label_end"):
        if column not in full.columns:
            raise ValueError(f"target output lacks column {column!r}")
    report = LeakageReport()
    decision = full.index.to_series()
    finite = np.isfinite(full["value"].to_numpy(dtype=np.float64))
    bad_start = finite & (full["label_start"] < decision).to_numpy()
    bad_end = finite & (full["label_end"] < full["label_start"]).to_numpy()
    for mask, detail in (
        (bad_start, "label_start before decision time"),
        (bad_end, "label_end before label_start"),
    ):
        if mask.any():
            report._add(Violation("label_bounds", "value", full.index[mask.argmax()], detail))

    rng = make_rng(seed)
    candidates = pd.DatetimeIndex(full.index[finite])
    report.points = _points(candidates, n_points, rng) if len(candidates) else []
    times = quotes[time_column]
    for t in report.points:
        row = full.loc[t]
        expected = float(row["value"])
        after_end = fn(quotes[times <= row["label_end"]], sigma)
        _compare_value(after_end, t, expected, "after_label_end", report)
        before = _perturb(quotes, (times < t).to_numpy(), rng, keep=time_column)
        _compare_value(fn(before, sigma), t, expected, "before_decision", report)
        others = sigma.index != t
        shaken = sigma.copy()
        shaken[others] = sigma[others] * np.exp(rng.normal(0, 0.5, size=int(others.sum())))
        _compare_value(fn(quotes, shaken), t, expected, "sigma_other_times", report)
    return report


def _checked_output(frame: pd.DataFrame, what: str) -> pd.DataFrame:
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError(f"{what} must be indexed by tz-aware decision times")
    if not index.is_monotonic_increasing or not index.is_unique:
        raise ValueError(f"{what} must have unique, increasing decision times")
    return frame


def _points(index: pd.DatetimeIndex, n_points: int, rng: np.random.Generator) -> list[pd.Timestamp]:
    if len(index) == 0:
        return []
    chosen = {0, len(index) - 1}
    if len(index) > 2 and n_points > 2:
        chosen.update(rng.choice(len(index), size=min(n_points - 2, len(index)), replace=False))
    return [index[i] for i in sorted(chosen)]


def _compare(
    full: pd.DataFrame, other: pd.DataFrame, t: pd.Timestamp, check: Check, report: LeakageReport
) -> None:
    expected = full.loc[full.index <= t]
    got = other.loc[other.index <= t]
    if not expected.index.equals(got.index):
        missing = expected.index.symmetric_difference(got.index)
        report._add(
            Violation(check, "<decision times>", missing.min(), "rows at or before t changed")
        )
        return
    for column in expected.columns:
        if column not in got.columns:
            report._add(Violation(check, column, t, "column missing when recomputed"))
            continue
        differs = _differs(expected[column], got[column])
        if differs.any():
            first = expected.index[int(differs.argmax())]
            report._add(
                Violation(
                    check,
                    column,
                    first,
                    f"value at {first} depends on data available only after {t}",
                )
            )


def _compare_value(
    frame: pd.DataFrame, t: pd.Timestamp, expected: float, check: Check, report: LeakageReport
) -> None:
    got = float(frame["value"].get(t, np.nan))
    if not np.isclose(got, expected, rtol=_RTOL, atol=_ATOL, equal_nan=True):
        report._add(Violation(check, "value", t, f"value changed from {expected} to {got}"))


def _differs(a: pd.Series, b: pd.Series) -> npt.NDArray[np.bool_]:
    numeric = pd.api.types.is_numeric_dtype(a.dtype) and pd.api.types.is_numeric_dtype(b.dtype)
    if numeric and not pd.api.types.is_bool_dtype(a.dtype):
        x = a.to_numpy(dtype=np.float64)
        y = b.to_numpy(dtype=np.float64)
        same: npt.NDArray[np.bool_] = np.isclose(x, y, rtol=_RTOL, atol=_ATOL, equal_nan=True)
        return ~same
    both_missing = (a.isna() & b.isna()).to_numpy()
    equal = (a == b).fillna(False).to_numpy(dtype=bool)
    return ~(equal | both_missing)


def _audit_provenance(full: pd.DataFrame, report: LeakageReport) -> None:
    decision = full.index.to_series()
    for column in full.columns:
        if not str(column).endswith(PROVENANCE_SUFFIX):
            continue
        late = (full[column] > decision).fillna(False).to_numpy(dtype=bool)
        if late.any():
            first = full.index[int(late.argmax())]
            report._add(
                Violation(
                    "availability",
                    str(column),
                    first,
                    f"input available at {full[column].iloc[int(late.argmax())]} used at {first}",
                )
            )


def _perturb(
    frame: pd.DataFrame,
    rows: npt.NDArray[np.bool_] | pd.Series,
    rng: np.random.Generator,
    *,
    keep: str,
) -> pd.DataFrame:
    """Copy of `frame` with numeric values randomized on `rows` (time columns untouched)."""
    mask = np.asarray(rows, dtype=bool)
    if not mask.any():
        return frame
    out = frame.copy()
    n = int(mask.sum())
    for column in frame.columns:
        dtype = frame[column].dtype
        if column == keep or not pd.api.types.is_numeric_dtype(dtype):
            continue
        values = frame[column].to_numpy(copy=True)
        if pd.api.types.is_bool_dtype(dtype):
            values[mask] = rng.random(n) < 0.5
        elif pd.api.types.is_integer_dtype(dtype):
            values[mask] = values[mask] + rng.integers(1, 10, size=n)
        else:
            values[mask] = values[mask] * np.exp(rng.normal(0, 0.1, size=n)) + rng.normal(
                0, 1e-3, size=n
            )
        out[column] = values
    return out


def _numeric_columns(frame: pd.DataFrame) -> list[str]:
    return [
        str(c)
        for c in frame.columns
        if pd.api.types.is_numeric_dtype(frame[c].dtype)
        and not pd.api.types.is_bool_dtype(frame[c].dtype)
    ]
