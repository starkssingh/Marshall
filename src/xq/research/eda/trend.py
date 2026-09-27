"""Trend and reversion descriptives (EDA-005). Descriptive only: nothing here is a test verdict.

- **Variance ratios** (Lo and MacKinlay 1988) of 1-bar log returns r_1..r_n over q bars, with
  overlapping q-bar sums: ``VR(q) = sigma_q^2 / sigma_1^2`` where
  ``sigma_1^2 = sum (r_t - mu)^2 / (n - 1)`` and
  ``sigma_q^2 = sum_{t=q}^{n} (sum_{j<q} r_{t-j} - q mu)^2 / m``,
  ``m = q (n - q + 1) (1 - q / n)``. VR above 1 describes persistence (trend), below 1 reversion;
  for serial correlations rho_k, ``VR(q) = 1 + 2 sum_{k<q} (1 - k / q) rho_k``. The
  heteroskedasticity-robust ``z* = sqrt(n) (VR(q) - 1) / sqrt(theta(q))`` uses
  ``theta(q) = sum_{j<q} (2 (q - j) / q)^2 delta_j`` with
  ``delta_j = n sum_{t>j} (r_t - mu)^2 (r_{t-j} - mu)^2 / (sum (r_t - mu)^2)^2``: it says how far
  VR is from 1 in standard errors, and is reported as a description, not a test.
- **Runs** of same-sign returns (zero returns skipped): the number of runs R, its expectation and
  standard deviation under independent signs (Wald and Wolfowitz: ``E[R] = 2 n+ n- / n + 1``,
  ``Var[R] = 2 n+ n- (2 n+ n- - n) / (n^2 (n - 1))``), their z, the mean run length and the run
  length counts. Fewer runs than expected describe persistence of signs, more describe reversal.
- **Buy-and-hold drawdowns** of the daily mid close (no costs): every episode from a peak to the
  trough and to recovery (the first close at or above the peak), with its depth and durations in
  trading days; the maximum drawdown; the share of trading days under water (below the running
  peak) and the longest underwater spell.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
import pandas as pd
from matplotlib.figure import Figure

from xq.research.reports import new_figure

FloatArray = npt.NDArray[np.float64]
MAX_RUN_BUCKET = 10


def variance_ratio(values: npt.ArrayLike, q: int) -> tuple[float, float]:
    """``(VR(q), z*)`` of 1-bar returns (module docstring); NaN when undefined."""
    r = np.asarray(values, dtype=np.float64)
    n = len(r)
    if q < 2 or n <= q:
        return math.nan, math.nan
    e = r - r.mean()
    total = float(e @ e)
    if total == 0:
        return math.nan, math.nan
    sigma_1 = total / (n - 1)
    cumulative = np.concatenate([[0.0], np.cumsum(e)])
    sums = cumulative[q:] - cumulative[:-q]
    m = q * (n - q + 1) * (1 - q / n)
    ratio = float(sums @ sums) / m / sigma_1
    squared = e**2
    theta = 0.0
    for j in range(1, q):
        delta = n * float(squared[j:] @ squared[:-j]) / total**2
        theta += (2 * (q - j) / q) ** 2 * delta
    z = math.sqrt(n) * (ratio - 1) / math.sqrt(theta) if theta > 0 else math.nan
    return ratio, z


def variance_ratio_table(values: npt.ArrayLike, qs: Sequence[int]) -> pd.DataFrame:
    """VR(q) and z* for every q."""
    rows = []
    for q in qs:
        ratio, z = variance_ratio(values, q)
        rows.append({"q": q, "variance_ratio": ratio, "z_robust": z})
    return pd.DataFrame(rows, columns=["q", "variance_ratio", "z_robust"])


def sign_runs(values: npt.ArrayLike) -> dict[str, float]:
    """Runs of same-sign non-zero returns (module docstring)."""
    signs = np.sign(np.asarray(values, dtype=np.float64))
    signs = signs[signs != 0]
    n = len(signs)
    up = int(np.sum(signs > 0))
    down = n - up
    if n < 2:
        return {
            "n": float(n),
            "runs": math.nan,
            "expected_runs": math.nan,
            "z": math.nan,
            "mean_run_length": math.nan,
            "share_up": math.nan,
        }
    runs = 1 + int(np.sum(signs[1:] != signs[:-1]))
    expected = 2 * up * down / n + 1
    variance = 2 * up * down * (2 * up * down - n) / (n**2 * (n - 1))
    return {
        "n": float(n),
        "runs": float(runs),
        "expected_runs": expected,
        "z": (runs - expected) / math.sqrt(variance) if variance > 0 else math.nan,
        "mean_run_length": n / runs,
        "share_up": up / n,
    }


def run_length_counts(values: npt.ArrayLike) -> pd.DataFrame:
    """Observed run lengths (1 .. MAX_RUN_BUCKET, the last bucket open) and the counts expected
    for independent signs with the observed up share (geometric run lengths)."""
    signs = np.sign(np.asarray(values, dtype=np.float64))
    signs = signs[signs != 0]
    lengths: list[int] = []
    if len(signs):
        change = np.flatnonzero(signs[1:] != signs[:-1]) + 1
        bounds = np.concatenate([[0], change, [len(signs)]])
        lengths = list(np.diff(bounds).astype(int))
    buckets = np.minimum(np.asarray(lengths, dtype=np.int64), MAX_RUN_BUCKET)
    observed = np.bincount(buckets, minlength=MAX_RUN_BUCKET + 1)[1:]
    up = float(np.mean(signs > 0)) if len(signs) else math.nan
    runs = len(lengths)
    expected = np.zeros(MAX_RUN_BUCKET)
    if runs and 0 < up < 1:
        # A run of the up (down) side continues with probability up (down); the sides alternate.
        for p_stay in (up, 1 - up):
            k = np.arange(1, MAX_RUN_BUCKET + 1)
            probability = (1 - p_stay) * p_stay ** (k - 1)
            probability[-1] = p_stay ** (MAX_RUN_BUCKET - 1)
            expected += runs / 2 * probability
    labels = [str(k) for k in range(1, MAX_RUN_BUCKET)] + [f"{MAX_RUN_BUCKET}+"]
    return pd.DataFrame({"run_length": labels, "observed": observed, "expected": expected})


def drawdown_episodes(prices: pd.Series) -> pd.DataFrame:
    """Every drawdown episode of a price series indexed by trading day (module docstring).

    Columns: ``peak``, ``trough``, ``recovery`` (None if not recovered), ``depth`` (fraction of the
    peak), ``days_to_trough`` (trading days from the peak), ``days_underwater`` (closes below the
    peak) and ``recovered``.
    """
    values = prices.to_numpy(dtype=np.float64)
    days = list(prices.index)
    rows = []
    peak_i = 0
    i = 1
    n = len(values)
    while i < n:
        if values[i] >= values[peak_i]:
            peak_i = i
            i += 1
            continue
        start = peak_i
        trough_i = i
        while i < n and values[i] < values[start]:
            if values[i] < values[trough_i]:
                trough_i = i
            i += 1
        recovered = i < n
        rows.append(
            {
                "peak": days[start],
                "trough": days[trough_i],
                "recovery": days[i] if recovered else None,
                "depth": 1 - values[trough_i] / values[start],
                "days_to_trough": trough_i - start,
                "days_underwater": (i if recovered else n) - start - 1,
                "recovered": recovered,
            }
        )
        if recovered:
            peak_i = i
            i += 1
    columns = [
        "peak",
        "trough",
        "recovery",
        "depth",
        "days_to_trough",
        "days_underwater",
        "recovered",
    ]
    return pd.DataFrame(rows, columns=columns)


def drawdown_summary(prices: pd.Series) -> dict[str, float]:
    """Maximum drawdown, share of days under water and the longest underwater spell (days)."""
    values = prices.to_numpy(dtype=np.float64)
    if len(values) == 0:
        return {
            "max_drawdown": math.nan,
            "share_underwater": math.nan,
            "longest_underwater_days": math.nan,
            "episodes": 0.0,
        }
    peak = np.maximum.accumulate(values)
    under = values < peak
    episodes = drawdown_episodes(prices)
    return {
        "max_drawdown": float(np.max(1 - values / peak)),
        "share_underwater": float(np.mean(under)),
        "longest_underwater_days": float(episodes["days_underwater"].max())
        if len(episodes)
        else 0.0,
        "episodes": float(len(episodes)),
    }


def underwater_figure(prices: pd.Series, title: str) -> Figure:
    """The daily mid close and its drawdown from the running peak."""
    values = prices.to_numpy(dtype=np.float64)
    x = pd.to_datetime(pd.Series(list(prices.index)))
    figure = new_figure(10, 6)
    top, bottom = figure.subplots(2, 1, sharex=True)
    top.plot(x, values, linewidth=1)
    top.set_ylabel("mid close")
    if len(values):
        bottom.fill_between(x, -(1 - values / np.maximum.accumulate(values)) * 100, 0, step="post")
    bottom.set_ylabel("drawdown (%)")
    figure.suptitle(title)
    return figure


def variance_ratio_figure(table: pd.DataFrame, title: str) -> Figure:
    """VR(q) against q, with the random-walk value 1."""
    figure = new_figure(6, 4)
    axes = figure.subplots()
    axes.plot(table["q"], table["variance_ratio"], "o-")
    axes.axhline(1.0, linewidth=0.8)
    axes.set_xscale("log")
    axes.set_xlabel("q (bars)")
    axes.set_ylabel("variance ratio")
    figure.suptitle(title)
    return figure
