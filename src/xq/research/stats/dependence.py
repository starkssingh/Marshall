"""Formal tests of serial dependence and volatility clustering (STAT-002).

For returns r (and |r|, r^2) with n values, autocorrelations rho_k as in EDA-003
(`xq.research.eda.dependence.autocorrelation`):

- **Ljung-Box** ``Q(m) = n (n + 2) sum_{k<=m} rho_k^2 / (n - k)``, chi-squared with m degrees of
  freedom under i.i.d. data (Ljung and Box 1978). On squared or absolute returns it tests for
  volatility clustering. On returns themselves it over-rejects when volatility clusters, because
  the i.i.d. variance of rho_k (1 / n) is too small; so returns are also tested with
- **the heteroskedasticity-robust portmanteau** ``Q*(m) = sum_{k<=m} rho_k^2 / se_k^2``, with the
  robust standard errors of EDA-003 (Diebold 1986; Lo and MacKinlay 1989's delta_k), also
  chi-squared with m degrees of freedom under the null of no autocorrelation (serially
  uncorrelated but possibly dependent returns). It is the one that speaks to return
  predictability; the plain Ljung-Box on returns is reported beside it.
- **ARCH-LM** (Engle 1982): regress e_t^2 on a constant and e_{t-1}^2 ... e_{t-q}^2 (e = r - mean),
  ``LM = (n - q) R^2``, chi-squared with q degrees of freedom. Rejection means volatility
  clustering, which is expected for gold and is **not** evidence of return predictability.

Several lags are tested per series, so each (test, series) family also carries Holm-adjusted
p-values across its lags (``detail_p_holm``); verdicts read the adjusted ones.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.stats import chi2

from xq.core.config import StatsDependenceConfig
from xq.research.eda.dependence import autocorrelation, robust_se
from xq.research.stats.results import StatResult, holm_adjust, results_table

FloatArray = npt.NDArray[np.float64]
MIN_OBS = 30
_TRANSFORMS: dict[str, Callable[[FloatArray], FloatArray]] = {
    "returns": lambda r: r,
    "abs_returns": np.abs,
    "squared_returns": np.square,
}


@dataclass(frozen=True)
class DependenceTests:
    """Ljung-Box, robust portmanteau and ARCH-LM results on one return series."""

    series: str
    results: tuple[StatResult, ...]

    def select(self, test: str, transform: str | None = None) -> list[StatResult]:
        """The results of `test` (optionally on one transform: ``returns``, ``squared_returns``)."""
        return [
            r
            for r in self.results
            if r.test == test and (transform is None or r.details.get("transform") == transform)
        ]

    def any_rejects(self, test: str, transform: str | None = None) -> bool:
        """True if any lag of the family rejects after the Holm adjustment."""
        return any(r.details["p_holm"] < r.alpha for r in self.select(test, transform))

    def table(self) -> pd.DataFrame:
        """One row per (test, transform, lag)."""
        return results_table(self.results)


def ljung_box(values: npt.ArrayLike, lag: int) -> tuple[float, float]:
    """``(Q, p-value)`` of the Ljung-Box test up to `lag`."""
    x = _check(values, lag)
    n = len(x)
    rho = autocorrelation(x, lag)
    q = float(n * (n + 2) * np.sum(rho**2 / (n - np.arange(1, lag + 1))))
    return q, float(chi2.sf(q, df=lag))


def robust_portmanteau(values: npt.ArrayLike, lag: int) -> tuple[float, float]:
    """``(Q*, p-value)`` of the heteroskedasticity-robust portmanteau test up to `lag`."""
    x = _check(values, lag)
    rho = autocorrelation(x, lag)
    se = robust_se(x, lag)
    if not np.all(np.isfinite(se)) or np.any(se == 0):
        return math.nan, math.nan
    q = float(np.sum((rho / se) ** 2))
    return q, float(chi2.sf(q, df=lag))


def arch_lm(values: npt.ArrayLike, lag: int) -> tuple[float, float]:
    """``(LM, p-value)`` of Engle's ARCH-LM test with `lag` lags of squared residuals."""
    x = _check(values, lag)
    e2 = (x - x.mean()) ** 2
    y = e2[lag:]
    columns = [np.ones(len(y))] + [e2[lag - k : len(e2) - k] for k in range(1, lag + 1)]
    design = np.column_stack(columns)
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    residual = y - design @ coef
    total = float(np.sum((y - y.mean()) ** 2))
    if total == 0:
        return math.nan, math.nan
    r2 = 1 - float(residual @ residual) / total
    lm = len(y) * r2
    return lm, float(chi2.sf(lm, df=lag))


def dependence_tests(
    returns: pd.Series | npt.ArrayLike, name: str, cfg: StatsDependenceConfig, alpha: float
) -> DependenceTests:
    """Every STAT-002 test on `returns` (module docstring).

    Raises:
        ValueError: if the returns have missing values or are too short for the largest lag.
    """
    r = np.asarray(returns, dtype=np.float64)
    results: list[StatResult] = []
    for transform, fn in _TRANSFORMS.items():
        x = fn(r)
        results.extend(
            _family(
                "LB",
                name,
                transform,
                [(lag, *ljung_box(x, lag)) for lag in cfg.ljung_box_lags],
                alpha,
                nobs=len(r),
                null="no autocorrelation up to the lag (i.i.d. variance of the autocorrelations)",
                assumptions=("i.i.d. under the null: over-rejects under volatility clustering",),
            )
        )
    results.extend(
        _family(
            "Q*",
            name,
            "returns",
            [(lag, *robust_portmanteau(r, lag)) for lag in cfg.ljung_box_lags],
            alpha,
            nobs=len(r),
            null="no autocorrelation up to the lag (heteroskedasticity-robust)",
            assumptions=(
                "uncorrelated but possibly dependent (e.g. GARCH) returns under the null",
            ),
        )
    )
    results.extend(
        _family(
            "ARCH-LM",
            name,
            "returns",
            [(lag, *arch_lm(r, lag)) for lag in cfg.arch_lm_lags],
            alpha,
            nobs=len(r),
            null="no ARCH effects: squared residuals are unpredictable from their lags",
            assumptions=(
                "constant mean",
                "LM = (n - q) R^2, chi-squared with q degrees of freedom",
            ),
        )
    )
    return DependenceTests(name, tuple(results))


def _family(
    test: str,
    name: str,
    transform: str,
    rows: list[tuple[int, float, float]],
    alpha: float,
    *,
    nobs: int,
    null: str,
    assumptions: tuple[str, ...],
) -> list[StatResult]:
    adjusted = holm_adjust([p for _, _, p in rows])
    return [
        StatResult(
            test=test,
            series=name,
            statistic=stat,
            p_value=p,
            lags=lag,
            nobs=nobs,
            null=null,
            alternative="dependence at some lag up to the lag",
            alpha=alpha,
            assumptions=(*assumptions, f"Holm across the lags {[r[0] for r in rows]}"),
            details={"transform": transform, "p_holm": float(adj)},
        )
        for (lag, stat, p), adj in zip(rows, adjusted, strict=True)
    ]


def _check(values: npt.ArrayLike, lag: int) -> FloatArray:
    x = np.asarray(values, dtype=np.float64)
    if not np.isfinite(x).all():
        raise ValueError("the series has missing or infinite values")
    if lag < 1:
        raise ValueError("lags must be at least 1")
    if len(x) < max(MIN_OBS, 2 * lag + 1):
        raise ValueError(f"{len(x)} observations are too few for {lag} lags")
    return x
