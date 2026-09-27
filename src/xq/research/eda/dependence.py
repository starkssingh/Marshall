"""Serial dependence of returns, absolute and squared returns (EDA-003).

For a series x (returns, |returns| or squared returns) with n values, demeaned as e = x - mean(x):

- **ACF** ``rho_k = sum_{t>k} e_t e_{t-k} / sum_t e_t^2`` (the biased estimator; statsmodels'
  ``acf`` with ``adjusted=False``), computed by FFT for lags 1..K.
- **PACF** by the Durbin-Levinson recursion on that ACF (statsmodels' ``pacf(method="ldb")``).
- **Bands.** The i.i.d. band ``z / sqrt(n)`` is wrong under conditional heteroskedasticity, which
  gold returns have: it is too narrow, so ordinary volatility clustering shows up as "significant"
  return autocorrelation. The heteroskedasticity-robust standard error of rho_k under the null of
  no autocorrelation (Taylor 1984; Lo and MacKinlay 1989's delta_k) is
  ``se_k = sqrt(sum_{t>k} e_t^2 e_{t-k}^2) / sum_t e_t^2``, which equals ``1 / sqrt(n)`` for
  i.i.d. data and is wider when volatility clusters. Both bands are reported at level
  ``dependence.ci_level``; lags whose |rho_k| exceeds the robust band are flagged. The PACF uses
  the same robust band (under the null the two coincide asymptotically). Bands are pointwise: with
  many lags a few exceedances are expected by chance.

Lags run up to one trading day of bars (23 market hours) and never fewer than
``dependence.min_lags``. Formal tests (Ljung-Box, ARCH-LM) are STAT-002.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
import pandas as pd
from matplotlib.figure import Figure
from scipy.stats import norm

from xq.research.reports import new_figure

FloatArray = npt.NDArray[np.float64]
SERIES = ("returns", "abs_returns", "squared_returns")


def autocorrelation(values: npt.ArrayLike, nlags: int) -> FloatArray:
    """Biased sample autocorrelations at lags 1..nlags (FFT; zero for a constant series)."""
    e = _demeaned(values)
    lags = _check_lags(nlags, len(e))
    covariance = _lagged_products(e, e, lags)
    if covariance[0] == 0:
        return np.zeros(lags)
    result: FloatArray = covariance[1:] / covariance[0]
    return result


def robust_se(values: npt.ArrayLike, nlags: int) -> FloatArray:
    """Heteroskedasticity-robust standard errors of the autocorrelations at lags 1..nlags."""
    e = _demeaned(values)
    lags = _check_lags(nlags, len(e))
    denominator = float(e @ e)
    if denominator == 0:
        return np.full(lags, math.nan)
    squared = e**2
    fourth = _lagged_products(squared, squared, lags)[1:]
    result: FloatArray = np.sqrt(np.maximum(fourth, 0.0)) / denominator
    return result


def partial_autocorrelation(acf: npt.ArrayLike) -> FloatArray:
    """Partial autocorrelations at lags 1..K from the autocorrelations (Durbin-Levinson)."""
    rho = np.asarray(acf, dtype=np.float64)
    lags = len(rho)
    pacf = np.zeros(lags)
    phi = np.zeros(0)
    variance = 1.0
    for k in range(lags):
        if variance <= 0:
            pacf[k:] = math.nan
            break
        current = (rho[k] - float(phi @ rho[:k][::-1])) / variance
        phi = np.concatenate([phi - current * phi[::-1], [current]])
        variance *= 1 - current**2
        pacf[k] = current
    return pacf


def lags_for(bars_per_day: int, min_lags: int, n: int) -> int:
    """One trading day of lags, at least `min_lags`, and fewer than the series length."""
    return max(0, min(max(bars_per_day, min_lags), n - 1))


def dependence_table(values: npt.ArrayLike, nlags: int, level: float) -> pd.DataFrame:
    """ACF, PACF, i.i.d. and robust bands of returns, |returns| and squared returns.

    One row per series and lag with columns ``series``, ``lag``, ``acf``, ``pacf``, ``iid_band``,
    ``robust_band``, ``robust_se`` and ``significant`` (|acf| above the robust band).
    """
    r = np.asarray(values, dtype=np.float64)
    z = float(norm.ppf(0.5 + level / 2))
    frames = []
    for name, series in zip(SERIES, (r, np.abs(r), r**2), strict=True):
        if nlags < 1 or len(series) < 3:
            continue
        acf = autocorrelation(series, nlags)
        se = robust_se(series, nlags)
        frames.append(
            pd.DataFrame(
                {
                    "series": name,
                    "lag": np.arange(1, nlags + 1),
                    "acf": acf,
                    "pacf": partial_autocorrelation(acf),
                    "iid_band": z / math.sqrt(len(series)),
                    "robust_band": z * se,
                    "robust_se": se,
                    "significant": np.abs(acf) > z * se,
                }
            )
        )
    columns = [
        "series",
        "lag",
        "acf",
        "pacf",
        "iid_band",
        "robust_band",
        "robust_se",
        "significant",
    ]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)


def dependence_summary(table: pd.DataFrame) -> pd.DataFrame:
    """Per series: lags, significant lags, first lags' ACF, and the robust / i.i.d. band ratio."""
    rows = []
    for name, chunk in table.groupby("series", sort=False):
        acf = chunk["acf"].to_numpy()
        rows.append(
            {
                "series": name,
                "lags": len(chunk),
                "significant_lags": int(chunk["significant"].sum()),
                "acf_1": float(acf[0]),
                "acf_2": float(acf[1]) if len(acf) > 1 else math.nan,
                "pacf_1": float(chunk["pacf"].iloc[0]),
                "band_ratio_mean": float(np.mean(chunk["robust_band"] / chunk["iid_band"])),
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "series",
            "lags",
            "significant_lags",
            "acf_1",
            "acf_2",
            "pacf_1",
            "band_ratio_mean",
        ],
    )


def dependence_figure(table: pd.DataFrame, title: str) -> Figure:
    """ACF of each series and PACF of returns, with i.i.d. and robust bands."""
    figure = new_figure(10, 7)
    grid = figure.subplots(2, 2)
    panels = [(s, "acf") for s in SERIES] + [("returns", "pacf")]
    for axes, (series, column) in zip(grid.flat, panels, strict=True):
        chunk = table.loc[table["series"] == series]
        if chunk.empty:
            continue
        lags = chunk["lag"].to_numpy()
        axes.vlines(lags, 0, chunk[column].to_numpy(), linewidth=1)
        axes.plot(lags, chunk["robust_band"], "--", linewidth=1, label="robust band")
        axes.plot(lags, -chunk["robust_band"], "--", linewidth=1)
        axes.plot(lags, chunk["iid_band"], ":", linewidth=1, label="i.i.d. band")
        axes.plot(lags, -chunk["iid_band"], ":", linewidth=1)
        axes.axhline(0, linewidth=0.5)
        axes.set_title(f"{column.upper()} of {series.replace('_', ' ')}")
        axes.set_xlabel("lag (bars)")
        axes.legend(fontsize="small")
    figure.suptitle(title)
    return figure


def _demeaned(values: npt.ArrayLike) -> FloatArray:
    x = np.asarray(values, dtype=np.float64)
    if np.isnan(x).any():
        raise ValueError("values must not contain missing values")
    result: FloatArray = x - x.mean() if len(x) else x
    return result


def _check_lags(nlags: int, n: int) -> int:
    if nlags < 1 or nlags >= n:
        raise ValueError(f"nlags must be between 1 and n - 1 ({n - 1}), got {nlags}")
    return nlags


def _lagged_products(a: FloatArray, b: FloatArray, lags: int) -> FloatArray:
    """``sum_{t>=k} a_t b_{t-k}`` for k = 0..lags, by FFT with zero padding (no wrap-around)."""
    n = len(a)
    size = 1 << math.ceil(math.log2(2 * n))
    spectrum = np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size))
    full = np.fft.irfft(spectrum, size)
    result: FloatArray = full[: lags + 1]
    return result
