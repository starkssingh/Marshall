"""Trailing range estimators and Wilder's ATR (VOL-001).

Bars are rows in time order with ``open``, ``high``, ``low`` and ``close`` of one price basis
(`price_columns` picks the basis: plain names, or ``bid_`` / ``ask_`` / ``mid_`` prefixed ones of a
bar set). With ``o = ln(O_t / C_{t-1})`` (the opening jump, which carries rollover and weekend
gaps), ``c = ln(C_t / O_t)``, ``h = ln(H_t / O_t)``, ``l = ln(L_t / O_t)`` and the close-to-close
return ``r = ln(C_t / C_{t-1})``, each estimator's per-bar variance over a trailing window of n bars
ending at t (inclusive) is:

- **close-to-close:** the sample variance of r (n - 1 denominator);
- **Parkinson (1980):** ``mean(ln(H / L)^2) / (4 ln 2)``;
- **Garman-Klass (1980):** ``mean(0.5 ln(H / L)^2 - (2 ln 2 - 1) c^2)``;
- **Rogers-Satchell (1991):** ``mean(h (h - c) + l (l - c))`` — unbiased under a drift;
- **Yang-Zhang (2000):** ``var(o) + k var(c) + (1 - k) RS`` with sample variances and
  ``k = 0.34 / (1.34 + (n + 1) / (n - 1))``; the only one that includes the opening jumps, so it
  is the one to use across the daily break and weekends.

The tables give sigma per bar (the square root), never annualized. **Wilder's ATR** smooths the
true range ``TR_t = max(H - L, |H - C_{t-1}|, |L - C_{t-1}|)`` with
``ATR_t = ATR_{t-1} + (TR_t - ATR_{t-1}) / n``, seeded with the mean of the first n true ranges;
it is in price units, so `range_estimators` also reports ``atr_rel = ATR / close`` — thresholds and
stops must use relative or volatility units, never dollars (CLAUDE.md).

Everything is **trailing**: the value at bar t uses bars up to t only (available at t's
``available_at``); it is missing until the window is full. Rows are taken as consecutive bars;
a missing bar simply makes the next opening jump longer.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import EstimatorsConfig

FloatArray = npt.NDArray[np.float64]
ESTIMATORS = ("close_to_close", "parkinson", "garman_klass", "rogers_satchell", "yang_zhang")
_LN2 = math.log(2.0)


def price_columns(prefix: str = "") -> tuple[str, str, str, str]:
    """The open, high, low and close columns of a price basis (``""``, ``"mid_"``, ...)."""
    return f"{prefix}open", f"{prefix}high", f"{prefix}low", f"{prefix}close"


def _ohlc(bars: pd.DataFrame, prefix: str) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
    columns = price_columns(prefix)
    missing = [c for c in columns if c not in bars.columns]
    if missing:
        raise KeyError(f"bars have no price columns {missing}")
    o, h, low, c = (bars[col].to_numpy(dtype=np.float64) for col in columns)
    if np.any((o <= 0) | (h <= 0) | (low <= 0) | (c <= 0)):
        raise ValueError("prices must be positive")
    if np.any((h < np.maximum(o, c)) | (low > np.minimum(o, c))):
        raise ValueError("every bar needs low <= open, close <= high")
    return o, h, low, c


def _rolling_mean(values: FloatArray, window: int) -> FloatArray:
    result: FloatArray = (
        pd.Series(values).rolling(window, min_periods=window).mean().to_numpy(dtype=np.float64)
    )
    return result


def _rolling_var(values: FloatArray, window: int) -> FloatArray:
    result: FloatArray = (
        pd.Series(values).rolling(window, min_periods=window).var(ddof=1).to_numpy(np.float64)
    )
    return result


def _previous(values: FloatArray) -> FloatArray:
    out = np.full(len(values), np.nan)
    out[1:] = values[:-1]
    return out


def close_to_close(bars: pd.DataFrame, window: int, *, prefix: str = "") -> FloatArray:
    """Per-bar variance of close-to-close log returns over the trailing window."""
    _, _, _, c = _ohlc(bars, prefix)
    return _rolling_var(np.log(c / _previous(c)), window)


def parkinson(bars: pd.DataFrame, window: int, *, prefix: str = "") -> FloatArray:
    """Parkinson's per-bar variance over the trailing window."""
    _, h, low, _ = _ohlc(bars, prefix)
    return _rolling_mean(np.log(h / low) ** 2 / (4 * _LN2), window)


def garman_klass(bars: pd.DataFrame, window: int, *, prefix: str = "") -> FloatArray:
    """Garman-Klass per-bar variance over the trailing window."""
    o, h, low, c = _ohlc(bars, prefix)
    term = 0.5 * np.log(h / low) ** 2 - (2 * _LN2 - 1) * np.log(c / o) ** 2
    return _rolling_mean(term, window)


def rogers_satchell(bars: pd.DataFrame, window: int, *, prefix: str = "") -> FloatArray:
    """Rogers-Satchell per-bar variance over the trailing window."""
    o, h, low, c = _ohlc(bars, prefix)
    return _rolling_mean(_rs_terms(o, h, low, c), window)


def yang_zhang(bars: pd.DataFrame, window: int, *, prefix: str = "") -> FloatArray:
    """Yang-Zhang per-bar variance over the trailing window (includes opening jumps)."""
    if window < 2:
        raise ValueError("Yang-Zhang needs a window of at least 2 bars")
    o, h, low, c = _ohlc(bars, prefix)
    opening = np.log(o / _previous(c))
    body = np.log(c / o)
    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    return (
        _rolling_var(opening, window)
        + k * _rolling_var(body, window)
        + (1 - k) * _rolling_mean(_rs_terms(o, h, low, c), window)
    )


def wilder_atr(bars: pd.DataFrame, window: int, *, prefix: str = "") -> FloatArray:
    """Wilder's average true range in price units (module docstring)."""
    if window < 1:
        raise ValueError("the ATR window must be at least 1")
    _, h, low, c = _ohlc(bars, prefix)
    previous = _previous(c)
    true_range = np.where(
        np.isnan(previous),
        h - low,
        np.maximum.reduce([h - low, np.abs(h - previous), np.abs(low - previous)]),
    )
    atr = np.full(len(true_range), np.nan)
    if len(true_range) >= window:
        atr[window - 1] = float(np.mean(true_range[:window]))
        for t in range(window, len(true_range)):
            atr[t] = atr[t - 1] + (true_range[t] - atr[t - 1]) / window
    return atr


def range_estimators(
    bars: pd.DataFrame, cfg: EstimatorsConfig, *, prefix: str = ""
) -> pd.DataFrame:
    """Sigma per bar of every estimator, Wilder's ATR and ATR relative to the close."""
    window = cfg.window
    variances = {
        "close_to_close": close_to_close(bars, window, prefix=prefix),
        "parkinson": parkinson(bars, window, prefix=prefix),
        "garman_klass": garman_klass(bars, window, prefix=prefix),
        "rogers_satchell": rogers_satchell(bars, window, prefix=prefix),
        "yang_zhang": yang_zhang(bars, window, prefix=prefix),
    }
    table = pd.DataFrame(
        {name: np.sqrt(np.maximum(v, 0.0)) for name, v in variances.items()}, index=bars.index
    )
    atr = wilder_atr(bars, cfg.atr_window, prefix=prefix)
    table["atr"] = atr
    table["atr_rel"] = atr / bars[price_columns(prefix)[3]].to_numpy(dtype=np.float64)
    return table


def _rs_terms(o: FloatArray, h: FloatArray, low: FloatArray, c: FloatArray) -> FloatArray:
    hi, lo, cl = np.log(h / o), np.log(low / o), np.log(c / o)
    result: FloatArray = hi * (hi - cl) + lo * (lo - cl)
    return result
