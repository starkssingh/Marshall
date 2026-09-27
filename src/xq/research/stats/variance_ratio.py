"""Variance-ratio tests: Lo-MacKinlay with the Chow-Denning joint test (STAT-003).

**Lo-MacKinlay.** For 1-bar log returns and a horizon of q bars, VR(q) and its
heteroskedasticity-robust z* are those of EDA-005 (`xq.research.eda.trend.variance_ratio`):
VR above 1 means persistence (trend), below 1 reversion; z* is asymptotically standard normal
under the null of serially uncorrelated (possibly heteroskedastic) returns. Here they are tests,
with two-sided p-values.

**Chow-Denning.** Testing several horizons one by one inflates the false-positive rate. The joint
test uses ``CD = max_q |z*(q)|`` over the m horizons (``stats.variance_ratio.horizons``, 2 to 64
bars) and the studentized maximum modulus bound with infinite degrees of freedom:
``p = 1 - (2 Phi(CD) - 1)^m`` (Chow and Denning 1993). A horizon is read as significant only if
the joint test rejects.

**Slices.** By session and by volatility regime the returns of a slice are not one contiguous
series, so `segmented_variance_ratio` sums q-bar windows only inside contiguous runs of the
slice (never across a gap), with the slice's own mean; with one run it equals the plain VR. The
slices' Chow-Denning p-values are Holm-adjusted across slices.

**Volatility regimes** (`volatility_regime_labels`) label each return by the trailing
volatility known before it — the RMS of the previous ``regime_window`` returns — cut at quantiles
computed on reference rows only (the caller's training or discovery rows), never on the rows being
labelled when those are later data.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.stats import norm

from xq.core.config import VarianceRatioConfig
from xq.research.stats.results import StatResult, holm_adjust, results_table

FloatArray = npt.NDArray[np.float64]
_NULL = "returns are serially uncorrelated: VR(q) = 1"


@dataclass(frozen=True)
class VarianceRatioTests:
    """Per-horizon Lo-MacKinlay tests and the Chow-Denning joint test on one series or slice."""

    series: str
    horizons: tuple[StatResult, ...]
    joint: StatResult

    def table(self) -> pd.DataFrame:
        """One row per horizon plus the joint test."""
        return results_table([*self.horizons, self.joint])


def segmented_variance_ratio(
    values: npt.ArrayLike, segments: npt.ArrayLike | None, q: int
) -> tuple[float, float]:
    """``(VR(q), z*)`` with q-bar windows inside contiguous segments only (module docstring).

    Args:
        values: 1-bar returns in time order.
        segments: A segment id per return (returns of one id must be contiguous and in order), or
            None for one contiguous series.
    """
    r = np.asarray(values, dtype=np.float64)
    n = len(r)
    ids = np.zeros(n, dtype=np.int64) if segments is None else _segment_codes(segments, n)
    if q < 2 or n <= q:
        return math.nan, math.nan
    e = r - r.mean()
    total = float(e @ e)
    if total == 0:
        return math.nan, math.nan
    sigma_1 = total / (n - 1)
    numerator = 0.0
    windows = 0
    starts = np.flatnonzero(np.r_[True, ids[1:] != ids[:-1]])
    ends = np.r_[starts[1:], n]
    for start, end in zip(starts, ends, strict=True):
        if end - start < q:
            continue
        cumulative = np.concatenate([[0.0], np.cumsum(e[start:end])])
        sums = cumulative[q:] - cumulative[:-q]
        numerator += float(sums @ sums)
        windows += len(sums)
    if windows == 0:
        return math.nan, math.nan
    m = q * windows * (1 - q / n)
    ratio = numerator / m / sigma_1
    squared = e**2
    theta = 0.0
    for j in range(1, q):
        within = ids[j:] == ids[:-j]  # pairs of returns inside one segment
        delta = n * float(np.sum(squared[j:][within] * squared[:-j][within])) / total**2
        theta += (2 * (q - j) / q) ** 2 * delta
    z = math.sqrt(n) * (ratio - 1) / math.sqrt(theta) if theta > 0 else math.nan
    return ratio, z


def variance_ratio_tests(
    values: npt.ArrayLike,
    name: str,
    horizons: Sequence[int],
    alpha: float,
    *,
    segments: npt.ArrayLike | None = None,
) -> VarianceRatioTests:
    """Lo-MacKinlay tests at every horizon and the Chow-Denning joint test (module docstring)."""
    r = np.asarray(values, dtype=np.float64)
    if not np.isfinite(r).all():
        raise ValueError("the returns have missing or infinite values")
    rows = []
    for q in horizons:
        ratio, z = segmented_variance_ratio(r, segments, q)
        p = 2 * float(norm.sf(abs(z))) if math.isfinite(z) else math.nan
        rows.append(
            StatResult(
                test=f"VR({q})",
                series=name,
                statistic=z,
                p_value=p,
                lags=q,
                nobs=len(r),
                null=_NULL,
                alternative="VR(q) != 1 (persistence above 1, reversion below)",
                alpha=alpha,
                assumptions=("heteroskedasticity-robust z* (Lo and MacKinlay 1988)",),
                details={"variance_ratio": ratio},
            )
        )
    finite = [abs(r.statistic) for r in rows if math.isfinite(r.statistic)]
    m = len(finite)
    if m:
        cd = max(finite)
        p_joint = 1 - (2 * float(norm.cdf(cd)) - 1) ** m
    else:
        cd, p_joint = math.nan, math.nan
    joint = StatResult(
        test="Chow-Denning",
        series=name,
        statistic=cd,
        p_value=p_joint,
        lags=None,
        nobs=len(r),
        null=f"{_NULL} at every horizon {list(horizons)}",
        alternative="VR(q) != 1 at some horizon",
        alpha=alpha,
        assumptions=(
            "studentized maximum modulus bound with infinite degrees of freedom",
            f"{m} horizons",
        ),
        details={"horizons": m},
    )
    return VarianceRatioTests(name, tuple(rows), joint)


def variance_ratio_by_slice(
    returns: pd.Series,
    labels: pd.Series,
    cfg: VarianceRatioConfig,
    alpha: float,
    *,
    name: str,
) -> pd.DataFrame:
    """VR tests per slice label (session, volatility regime), Holm-adjusted across slices.

    Args:
        returns: 1-bar returns in time order.
        labels: A slice label per return (same index); missing labels are left out.

    Returns:
        One row per (slice, test) with ``p_holm_slices`` on the Chow-Denning rows.
    """
    if not returns.index.equals(labels.index):
        raise ValueError("returns and labels must share one index")
    values = returns.to_numpy(dtype=np.float64)
    keys = labels.to_numpy(dtype=object)
    frames = []
    joints: list[float] = []
    for label in sorted({k for k in keys if not pd.isna(k)}, key=str):
        mask = keys == label
        positions = np.flatnonzero(mask)
        runs = np.cumsum(np.r_[True, np.diff(positions) != 1])  # contiguous runs of the slice
        tests = variance_ratio_tests(
            values[mask], f"{name}[{label}]", cfg.horizons, alpha, segments=runs
        )
        table = tests.table()
        table.insert(0, "slice", str(label))
        frames.append(table)
        joints.append(tests.joint.p_value)
    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True)
    adjusted = holm_adjust(joints)
    result["p_holm_slices"] = math.nan
    joint_rows = np.flatnonzero(result["test"].to_numpy() == "Chow-Denning")
    result.loc[joint_rows, "p_holm_slices"] = adjusted
    return result


def volatility_regime_labels(
    returns: pd.Series,
    window: int,
    quantiles: Sequence[float],
    *,
    reference: npt.ArrayLike | None = None,
) -> pd.Series:
    """Label each return by the trailing volatility known before it (module docstring).

    Args:
        returns: 1-bar returns in time order.
        window: Returns in the trailing RMS (the current return is excluded).
        quantiles: Cut-offs as quantiles of the trailing volatility on the reference rows.
        reference: Boolean mask of the rows whose trailing volatility sets the cut-offs (default:
            all rows — only for a purely descriptive reading of one sample).

    Returns:
        ``"v0"`` (calmest) ... ``"v<k>"`` per return; missing until `window` earlier returns exist.
    """
    r = returns.to_numpy(dtype=np.float64)
    squared = pd.Series(r**2)
    trailing = np.sqrt(squared.rolling(window, min_periods=window).mean().shift(1).to_numpy())
    mask = np.ones(len(r), dtype=bool) if reference is None else np.asarray(reference, bool)
    usable = trailing[mask & np.isfinite(trailing)]
    if len(usable) == 0:
        raise ValueError("no reference row has a trailing volatility")
    cuts = np.quantile(usable, list(quantiles))
    codes = np.searchsorted(cuts, trailing, side="right")
    labels = np.array([f"v{c}" for c in codes], dtype=object)
    labels[~np.isfinite(trailing)] = None
    return pd.Series(labels, index=returns.index, name="vol_regime")


def _segment_codes(segments: npt.ArrayLike, n: int) -> npt.NDArray[np.int64]:
    raw = np.asarray(segments)
    if len(raw) != n:
        raise ValueError(f"{len(raw)} segment ids for {n} returns")
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    change = np.r_[True, raw[1:] != raw[:-1]]
    codes = np.cumsum(change) - 1
    if len(np.unique(raw)) != int(codes[-1]) + 1:
        raise ValueError("each segment's returns must be contiguous")
    return codes.astype(np.int64)
