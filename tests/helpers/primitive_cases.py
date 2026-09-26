"""Every causal primitive (DS-003) as a series-to-series function, for the truncation-invariance
property test and the leakage suite (DS-006). A new primitive must be added here."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from xq.datasets import primitives as p

SeriesFn = Callable[[pd.Series], pd.Series]


def _prices(series: pd.Series) -> pd.Series:
    return series.abs() + 1.0


def _returns(series: pd.Series) -> pd.Series:
    return p.log_returns(_prices(series))


PRIMITIVE_CASES: dict[str, SeriesFn] = {
    "rolling_mean_rows": lambda s: p.rolling(s, 5, "mean", min_periods=2),
    "rolling_std_rows": lambda s: p.rolling(s, 4, "std"),
    "rolling_median_rows": lambda s: p.rolling(s, 6, "median", min_periods=1),
    "rolling_sum_time": lambda s: p.rolling(s, "30min", "sum"),
    "rolling_max_time": lambda s: p.rolling(s, "45min", "max", min_periods=2),
    "rolling_var_time": lambda s: p.rolling(s, "1h", "var", min_periods=3),
    "expanding_mean": lambda s: p.expanding(s, "mean"),
    "expanding_std": lambda s: p.expanding(s, "std", min_periods=2),
    "ewm_mean_span": lambda s: p.ewm_mean(s, span=8),
    "ewm_mean_halflife": lambda s: p.ewm_mean(s, halflife=3.0, min_periods=2),
    "ewm_std": lambda s: p.ewm_std(s, span=10),
    "log_returns": lambda s: p.log_returns(_prices(s), 3),
    "simple_returns": lambda s: p.simple_returns(_prices(s), 2),
    "vol_normalized": lambda s: p.vol_normalized(
        _returns(s), p.ewma_volatility(_returns(s), span=10)
    ),
    "realized_volatility_rows": lambda s: p.realized_volatility(_returns(s), 5),
    "realized_volatility_time": lambda s: p.realized_volatility(_returns(s), "1h"),
    "ewma_volatility": lambda s: p.ewma_volatility(_returns(s), span=12),
    "lag": lambda s: p.lag(s, 2),
}


def random_series(seed: int, n: int = 200, nan_share: float = 0.1) -> pd.Series:
    """Irregularly spaced tz-aware series with sporadic missing values."""
    rng = np.random.default_rng(seed)
    gaps = rng.integers(1, 600, size=n)  # seconds between rows
    index = pd.Timestamp("2024-03-11", tz="UTC") + pd.to_timedelta(np.cumsum(gaps), unit="s")
    values = 100 + np.cumsum(rng.normal(0, 1, size=n))
    values[rng.random(n) < nan_share] = np.nan
    return pd.Series(values, index=pd.DatetimeIndex(index), name="x")
