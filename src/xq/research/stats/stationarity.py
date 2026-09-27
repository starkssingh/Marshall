"""Stationarity battery with a joint verdict (STAT-001).

Four tests run on one series (log price, log returns or log realized volatility), all with the
deterministic terms of ``stats.stationarity.trend`` (a level by default):

- **ADF** (augmented Dickey-Fuller; lags chosen by ``adf_lag_method``, AIC by default, up to
  ``adf_max_lags``): H0 unit root, H1 stationary. MacKinnon p-values (arch).
- **Phillips-Perron** (Newey-West long-run variance): H0 unit root, H1 stationary. Robust to
  serial correlation and heteroskedasticity of the errors without lag augmentation (arch).
- **KPSS** around a level (``c``) and, reported beside it, around a trend (``ct``): H0 stationary,
  H1 unit root; the bandwidth is chosen from the data (Hobijn, Franses and Ooms 1998; arch's
  default, whose deprecation notice about the change is expected and not recorded). p-values are
  interpolated in a table of simulated quantiles, so they are flat beyond its ends.
- **Zivot-Andrews** with one break in the level (``trim`` of the sample excluded at each end from
  the break search, AIC lags): H0 unit root without a break, H1 stationary around one break at an
  unknown date, reported with its date (statsmodels).

**Joint verdict** (the plan's rule: read unit-root tests with KPSS, never alone), from ADF, PP and
the level KPSS at level ``alpha``:

| ADF and PP | KPSS (level) | verdict |
| --- | --- | --- |
| both reject | does not reject | ``stationary`` |
| neither rejects | rejects | ``unit_root`` |
| both reject | rejects | ``conflicting`` (long memory, a break or near-unit-root dynamics) |
| neither rejects | does not reject | ``inconclusive`` (low power; the data cannot tell) |
| they disagree | either | ``mixed`` |

When Zivot-Andrews rejects while the verdict is ``unit_root`` or ``conflicting``, the explanation
adds that the series may be stationary around a break at the reported date (a single search for
one break, not a search over many). Non-rejection is never read as proof of a unit root.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
from arch.unitroot import ADF, KPSS, PhillipsPerron
from statsmodels.tsa.stattools import zivot_andrews

from xq.core.config import StationarityConfig
from xq.research.stats.results import StatResult, captured_warnings, results_table

FloatArray = npt.NDArray[np.float64]
Verdict = Literal["stationary", "unit_root", "conflicting", "inconclusive", "mixed"]
Trend = Literal["n", "c", "ct"]
LagMethod = Literal["aic", "bic", "t-stat"]
MIN_OBS = 50
_KPSS_LAG_NOTICE = "Lag selection has changed"
_UNIT_ROOT = "the series has a unit root"
_STATIONARY = "the series is stationary"
_TREND_TEXT = {"n": "no deterministic terms", "c": "a constant", "ct": "a constant and a trend"}


@dataclass(frozen=True)
class StationarityBattery:
    """The four tests on one series and their joint reading."""

    series: str
    results: tuple[StatResult, ...]
    verdict: Verdict
    explanation: str
    break_date: pd.Timestamp | int | None

    def result(self, test: str) -> StatResult:
        """The result of `test` (e.g. ``"ADF"``, ``"KPSS(c)"``)."""
        for result in self.results:
            if result.test == test:
                return result
        raise KeyError(f"no {test} result for {self.series}")

    def table(self) -> pd.DataFrame:
        """One row per test."""
        return results_table(self.results)


def stationarity_battery(
    series: pd.Series | npt.ArrayLike, name: str, cfg: StationarityConfig, alpha: float
) -> StationarityBattery:
    """Run ADF, PP, KPSS (level and trend) and Zivot-Andrews on `series` (module docstring).

    Args:
        series: The values in time order; with a DatetimeIndex, the break is reported as a date.
        name: What the series is (``"log_price"``, ``"log_returns"``, ``"log_rv_1d"``).

    Raises:
        ValueError: if the series has missing or infinite values or fewer than 50 observations.
    """
    values, index = _values(series)
    trend = cfg.trend
    results = [
        adf_test(
            values,
            name,
            trend=trend,
            method=cfg.adf_lag_method,
            max_lags=cfg.adf_max_lags,
            alpha=alpha,
        ),
        pp_test(values, name, trend=trend, alpha=alpha),
        *(kpss_test(values, name, trend=t, alpha=alpha) for t in cfg.kpss_trends),
    ]
    za = zivot_andrews_test(
        values,
        name,
        trend=trend,
        trim=cfg.zivot_andrews_trim,
        max_lags=cfg.adf_max_lags,
        alpha=alpha,
        index=index,
    )
    results.append(za)
    verdict, explanation = joint_verdict(results[0], results[1], _find(results, "KPSS(c)"), za)
    return StationarityBattery(
        name, tuple(results), verdict, explanation, za.details.get("break_date")
    )


def adf_test(
    values: FloatArray,
    name: str,
    *,
    trend: Trend,
    method: LagMethod,
    max_lags: int | None,
    alpha: float,
) -> StatResult:
    """Augmented Dickey-Fuller test (H0: unit root)."""
    notes: list[str] = []
    with captured_warnings(notes):
        test = ADF(values, trend=trend, method=method, max_lags=max_lags)
        stat, p_value, lags = float(test.stat), float(test.pvalue), int(test.lags)
        critical = {k: float(v) for k, v in test.critical_values.items()}
    return StatResult(
        test="ADF",
        series=name,
        statistic=stat,
        p_value=p_value,
        lags=lags,
        nobs=int(test.nobs),
        null=_UNIT_ROOT,
        alternative=_STATIONARY,
        alpha=alpha,
        assumptions=(
            f"deterministic terms: {_TREND_TEXT[trend]}",
            f"lag order chosen by {method.upper()}",
            "MacKinnon (1994, 2010) p-values",
        ),
        details={f"cv_{k}": v for k, v in critical.items()},
        notes=tuple(notes),
    )


def pp_test(values: FloatArray, name: str, *, trend: Trend, alpha: float) -> StatResult:
    """Phillips-Perron Z-tau test (H0: unit root)."""
    notes: list[str] = []
    with captured_warnings(notes):
        test = PhillipsPerron(values, trend=trend, test_type="tau")
        stat, p_value, lags = float(test.stat), float(test.pvalue), int(test.lags)
    return StatResult(
        test="PP",
        series=name,
        statistic=stat,
        p_value=p_value,
        lags=lags,
        nobs=int(test.nobs),
        null=_UNIT_ROOT,
        alternative=_STATIONARY,
        alpha=alpha,
        assumptions=(
            f"deterministic terms: {_TREND_TEXT[trend]}",
            "Newey-West long-run variance (Bartlett kernel)",
            "MacKinnon p-values",
        ),
        notes=tuple(notes),
    )


def kpss_test(
    values: FloatArray, name: str, *, trend: Literal["c", "ct"], alpha: float
) -> StatResult:
    """KPSS test (H0: stationary around a level ``c`` or a trend ``ct``)."""
    notes: list[str] = []
    with captured_warnings(notes), warnings.catch_warnings():
        # arch announces that its default bandwidth is now data-dependent: that default is used
        warnings.filterwarnings("ignore", message=_KPSS_LAG_NOTICE, category=DeprecationWarning)
        test = KPSS(values, trend=trend)
        stat, p_value, lags = float(test.stat), float(test.pvalue), int(test.lags)
    around = "a level" if trend == "c" else "a linear trend"
    return StatResult(
        test=f"KPSS({trend})",
        series=name,
        statistic=stat,
        p_value=p_value,
        lags=lags,
        nobs=int(test.nobs),
        null=f"the series is stationary around {around}",
        alternative="the series has a unit root",
        alpha=alpha,
        assumptions=(
            "bandwidth chosen from the data (Hobijn, Franses and Ooms 1998)",
            "p-values linearly interpolated in arch's simulated KPSS quantile table (flat beyond "
            "its ends)",
        ),
        notes=tuple(notes),
    )


def zivot_andrews_test(
    values: FloatArray,
    name: str,
    *,
    trend: Trend,
    trim: float,
    max_lags: int | None,
    alpha: float,
    index: pd.DatetimeIndex | None = None,
) -> StatResult:
    """Zivot-Andrews test with one break (H0: unit root without a break)."""
    notes: list[str] = []
    regression = "c" if trend in ("n", "c") else "ct"
    with captured_warnings(notes):
        stat, p_value, critical, lags, position = zivot_andrews(
            values, trim=trim, maxlag=max_lags, regression=regression, autolag="AIC"
        )
    position = int(position)
    break_at: pd.Timestamp | int = index[position] if index is not None else position
    return StatResult(
        test="ZA",
        series=name,
        statistic=float(stat),
        p_value=float(p_value),
        lags=int(lags),
        nobs=len(values),
        null="the series has a unit root without a structural break",
        alternative="the series is stationary around one break in its level",
        alpha=alpha,
        assumptions=(
            f"one break searched over the middle {1 - 2 * trim:.0%} of the sample",
            "lag order chosen by AIC",
            "p-values interpolated from simulated critical values",
        ),
        details={
            "break_position": position,
            "break_date": break_at,
            **{f"cv_{k}": float(v) for k, v in critical.items()},
        },
        notes=tuple(notes),
    )


def joint_verdict(
    adf: StatResult, pp: StatResult, kpss: StatResult, za: StatResult | None = None
) -> tuple[Verdict, str]:
    """The joint reading of ADF, PP and the level KPSS (table in the module docstring)."""
    if adf.reject != pp.reject:
        return "mixed", (
            "ADF and Phillips-Perron disagree about a unit root; neither reading is supported"
        )
    unit_root_rejected = adf.reject
    verdict: Verdict
    if unit_root_rejected and not kpss.reject:
        verdict, text = "stationary", "ADF and PP reject a unit root and KPSS does not reject"
    elif not unit_root_rejected and kpss.reject:
        verdict, text = (
            "unit_root",
            ("ADF and PP do not reject a unit root and KPSS rejects stationarity"),
        )
    elif unit_root_rejected and kpss.reject:
        verdict, text = (
            "conflicting",
            (
                "ADF and PP reject a unit root but KPSS also rejects stationarity: consistent with "
                "long memory, a structural break or near-unit-root dynamics"
            ),
        )
    else:
        verdict, text = (
            "inconclusive",
            ("no test rejects: the tests lack the power to tell a unit root from stationarity"),
        )
    if za is not None and za.reject and verdict in ("unit_root", "conflicting"):
        text += (
            f"; Zivot-Andrews rejects a unit root against one break (at "
            f"{za.details.get('break_date')}), so the series may be stationary around a break"
        )
    return verdict, text


def _find(results: list[StatResult], test: str) -> StatResult:
    return next(r for r in results if r.test == test)


def _values(series: pd.Series | npt.ArrayLike) -> tuple[FloatArray, pd.DatetimeIndex | None]:
    index = None
    if isinstance(series, pd.Series):
        if isinstance(series.index, pd.DatetimeIndex):
            index = series.index
        values = series.to_numpy(dtype=np.float64)
    else:
        values = np.asarray(series, dtype=np.float64)
    if values.ndim != 1:
        raise ValueError("a stationarity test needs one series")
    if not np.isfinite(values).all():
        raise ValueError("the series has missing or infinite values; drop or explain them first")
    if len(values) < MIN_OBS:
        raise ValueError(f"the series has {len(values)} observations; at least {MIN_OBS} needed")
    if math.isclose(float(np.std(values)), 0.0):
        raise ValueError("the series is constant")
    return values, index
