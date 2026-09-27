"""Return distributions, tails and their stability (EDA-002).

For the log returns of each timeframe (1m to 1d):

- **Moments** — mean, standard deviation (``ddof=1``), skewness and excess kurtosis (the moment
  estimators of `scipy.stats.skew` and `scipy.stats.kurtosis`, ``bias=True``, Fisher's
  definition), each with a stationary-bootstrap percentile interval (`xq.research.eda.bootstrap`).
- **Jarque-Bera** — `scipy.stats.jarque_bera`: the statistic and its asymptotic p-value.
- **Student-t fit** — maximum likelihood (`scipy.stats.t.fit`): degrees of freedom, location and
  scale, for the QQ plot against Student-t beside the one against the normal.
- **Hill tail index** — for each tail, from the ``k = floor(hill_tail_fraction * n)`` largest
  magnitudes x(1) >= ... >= x(k+1) of that tail: ``alpha = 1 / mean(log(x(i) / x(k+1)))``,
  i = 1..k. A power-law tail with P(X > x) ~ x^-alpha gives alpha; the normal has no finite index
  (the estimate grows with n).
- **Stability by year** — the moments per calendar year of the trading day.

Everything is descriptive; nothing here is fitted for later use.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np
import numpy.typing as npt
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from scipy import stats

from xq.research.eda.bootstrap import bootstrap_intervals
from xq.research.reports import new_figure

FloatArray = npt.NDArray[np.float64]
MOMENTS = ("mean", "std", "skew", "excess_kurtosis")
#: Order statistics drawn in a QQ plot (plus the `QQ_TAIL` most extreme of each tail).
QQ_POINTS = 2000
QQ_TAIL = 50
_BPS = 1e4


def moments(values: npt.ArrayLike) -> dict[str, float]:
    """Mean, standard deviation, skewness and excess kurtosis (module docstring).

    Skewness and kurtosis are the moment estimators ``m3 / m2^1.5`` and ``m4 / m2^2 - 3`` with
    central moments ``m_k = mean((x - mean)^k)``: scipy's ``skew`` and ``kurtosis`` with
    ``bias=True``, computed directly because the bootstrap calls this thousands of times.
    """
    x = np.asarray(values, dtype=np.float64)
    if len(x) < 2:
        return dict.fromkeys(MOMENTS, math.nan)
    mean = float(np.mean(x))
    e = x - mean
    squared = e * e
    m2 = float(np.mean(squared))
    if m2 == 0:
        return {"mean": mean, "std": 0.0, "skew": math.nan, "excess_kurtosis": math.nan}
    return {
        "mean": mean,
        "std": math.sqrt(m2 * len(x) / (len(x) - 1)),
        "skew": float(np.mean(squared * e)) / m2**1.5,
        "excess_kurtosis": float(np.mean(squared * squared)) / m2**2 - 3.0,
    }


def jarque_bera(values: npt.ArrayLike) -> tuple[float, float]:
    """Jarque-Bera statistic and asymptotic p-value (scipy)."""
    x = np.asarray(values, dtype=np.float64)
    if len(x) < 3 or np.std(x) == 0:
        return math.nan, math.nan
    result = stats.jarque_bera(x)
    return float(result.statistic), float(result.pvalue)


def hill_tail_index(magnitudes: npt.ArrayLike, k: int) -> float:
    """Hill estimate of the tail index from the `k` largest of positive `magnitudes`."""
    x = np.sort(np.asarray(magnitudes, dtype=np.float64))[::-1]
    x = x[x > 0]
    if k < 2 or len(x) <= k:
        return math.nan
    logs = np.log(x[:k] / x[k])
    mean_log = float(np.mean(logs))
    return 1.0 / mean_log if mean_log > 0 else math.nan


def tail_indices(values: npt.ArrayLike, fraction: float) -> tuple[float, float]:
    """Hill indices of the left and right tails; ``k = floor(fraction * n)`` of each tail."""
    x = np.asarray(values, dtype=np.float64)
    k = math.floor(fraction * len(x))
    return hill_tail_index(-x[x < 0], k), hill_tail_index(x[x > 0], k)


def fit_student_t(values: npt.ArrayLike) -> tuple[float, float, float]:
    """Maximum-likelihood Student-t fit: degrees of freedom, location, scale."""
    x = np.asarray(values, dtype=np.float64)
    if len(x) < 10 or np.std(x) == 0:
        return math.nan, math.nan, math.nan
    df, loc, scale = stats.t.fit(x)
    return float(df), float(loc), float(scale)


def distribution_row(
    values: npt.ArrayLike,
    *,
    hill_fraction: float,
    n_boot: int,
    mean_block: float,
    level: float,
    seed: int,
) -> dict[str, float]:
    """Every EDA-002 statistic of one return series, returns in basis points."""
    x = np.asarray(values, dtype=np.float64) * _BPS
    row: dict[str, float] = {"n": float(len(x)), **moments(x)}
    intervals = bootstrap_intervals(
        x, moments, n_boot=n_boot, mean_block=mean_block, level=level, seed=seed
    )
    for name in MOMENTS:
        row[f"{name}_ci_low"], row[f"{name}_ci_high"] = intervals[name]
    row["jb_stat"], row["jb_p"] = jarque_bera(x)
    row["hill_left"], row["hill_right"] = tail_indices(x, hill_fraction)
    row["t_df"], row["t_loc"], row["t_scale"] = fit_student_t(x)  # in bps
    row["block_length"] = mean_block
    return row


def yearly_moments(returns: pd.DataFrame) -> pd.DataFrame:
    """Moments of ``ret`` (in bps) per calendar year of ``trading_day``."""
    years = pd.Series([d.year for d in returns["trading_day"]], index=returns.index)
    rows = []
    for year, chunk in returns.groupby(years, sort=True):
        rows.append({"year": int(year), "n": len(chunk), **moments(chunk["ret"].to_numpy() * _BPS)})
    return pd.DataFrame(rows, columns=["year", "n", *MOMENTS])


def qq_figure(values: npt.ArrayLike, t_params: tuple[float, float, float], title: str) -> Figure:
    """QQ plots of returns (log) against the fitted normal and Student-t (`t_params` in bps).

    Long series are thinned to about `QQ_POINTS` order statistics, keeping the most extreme ones.
    """
    x = np.sort(np.asarray(values, dtype=np.float64) * _BPS)
    n = len(x)
    keep = np.unique(
        np.concatenate(
            [
                np.round(np.linspace(0, n - 1, min(n, QQ_POINTS))).astype(np.int64),
                np.arange(min(n, QQ_TAIL)),
                np.arange(max(0, n - QQ_TAIL), n),
            ]
        )
    )
    probabilities = (keep + 0.5) / n
    sample = x[keep]
    figure = new_figure(10, 4.5)
    left, right = figure.subplots(1, 2)
    mean, std = (float(np.mean(x)), float(np.std(x, ddof=1))) if n > 1 else (0.0, 1.0)
    _qq(left, stats.norm.ppf(probabilities, loc=mean, scale=std), sample, "normal")
    df, loc, scale = t_params
    if math.isfinite(df):
        student = stats.t.ppf(probabilities, df, loc=loc, scale=scale)
        _qq(right, student, sample, f"Student-t (df {df:.2f})")
    figure.suptitle(title)
    return figure


def _qq(axes: Axes, theoretical: FloatArray, sample: FloatArray, name: str) -> None:
    axes.plot(theoretical, sample, ".", markersize=2)
    if len(sample):
        low = float(min(theoretical[0], sample[0]))
        high = float(max(theoretical[-1], sample[-1]))
        axes.plot([low, high], [low, high], "-", linewidth=1)
    axes.set_xlabel(f"{name} quantile (bps)")
    axes.set_ylabel("return quantile (bps)")
    axes.set_title(f"against the fitted {name}")


def distribution_summary(rows: Mapping[str, Mapping[str, float]]) -> pd.DataFrame:
    """One row per timeframe, in the given order."""
    return pd.DataFrame([{"timeframe": tf, **row} for tf, row in rows.items()])
