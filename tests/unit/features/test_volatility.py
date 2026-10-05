"""FEAT-004: volatility features against hand computations (Wilder's ATR step by step)."""

import numpy as np
import pytest

from helpers.features import hand_bars, run
from xq.core.errors import ConfigError
from xq.features.base import bar_sigma
from xq.features.volatility import (
    atr,
    consistent,
    ewma_sigma,
    range_expansion,
    range_vol,
    vol_of_vol,
    vol_ratio,
)


def test_wilder_atr_by_hand_relative_to_the_close() -> None:
    bars = hand_bars(
        [10.0, 11.0, 11.0, 13.0],
        open_=[10.0, 10.0, 11.0, 11.0],
        high=[11.0, 12.0, 11.5, 14.0],
        low=[9.0, 10.0, 10.5, 11.0],
    )
    # true ranges: 2 (first bar: high - low), max(2, |12-10|, |10-10|) = 2,
    # max(1, 0.5, 0.5) = 1, max(3, |14-11|, |11-11|) = 3
    out = run(atr, bars, window=3)["value"]
    first = (2 + 2 + 1) / 3  # seeded with the mean of the first three true ranges
    second = first + (3 - first) / 3  # Wilder's smoothing: 19 / 9
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(first / 11.0)
    assert out.iloc[3] == pytest.approx(second / 13.0)
    assert second == pytest.approx(19 / 9)


def test_parkinson_and_close_to_close_estimators_by_hand() -> None:
    close = [100.0, 101.0, 100.5, 102.0]
    high, low = [100.5, 101.5, 101.5, 103.0], [99.5, 100.0, 100.0, 100.4]
    bars = hand_bars(close, high=high, low=low)
    parkinson = run(range_vol, bars, estimator="parkinson", window=2)["value"]
    hl = np.log(np.array(high) / np.array(low)) ** 2 / (4 * np.log(2))
    assert parkinson.iloc[0] != parkinson.iloc[0]  # missing until the window is full
    assert parkinson.iloc[3] == pytest.approx(np.sqrt((hl[2] + hl[3]) / 2))
    c2c = run(range_vol, bars, estimator="close_to_close", window=2)["value"]
    r = np.diff(np.log(close))
    assert c2c.iloc[:2].isna().all()  # two returns need three closes
    assert c2c.iloc[3] == pytest.approx(np.std(r[1:], ddof=1))
    with pytest.raises(ConfigError):
        range_vol.validate({"estimator": "magic", "window": 20})


def test_ratio_and_volatility_of_volatility() -> None:
    rng = np.random.default_rng(5)
    close = list(2000 * np.exp(np.cumsum(rng.normal(0, 0.001, 30))))
    r = np.diff(np.log(close))
    ratio = run(vol_ratio, hand_bars(close), short=3, long=6)["value"]
    assert ratio.iloc[:6].isna().all()
    assert ratio.iloc[20] == pytest.approx(np.std(r[17:20], ddof=1) / np.std(r[14:20], ddof=1))
    vov = run(vol_of_vol, hand_bars(close), window=4, inner=3)["value"]
    logs = [np.log(np.std(r[k - 3 : k], ddof=1)) for k in range(17, 21)]
    assert vov.iloc[:6].isna().all()
    assert vov.iloc[20] == pytest.approx(np.std(logs, ddof=1))
    with pytest.raises(ConfigError, match="long must be longer"):
        vol_ratio.validate({"short": 5, "long": 5})


def test_ewma_sigma_and_range_expansion() -> None:
    close = [100.0, 101.0, 100.5, 102.0]
    high, low = [101.0, 102.0, 101.0, 104.0], [99.0, 100.0, 100.0, 101.0]
    bars = hand_bars(close, high=high, low=low)
    sigma = run(ewma_sigma, bars, span=2)["value"]
    assert sigma.equals(bar_sigma(bars, 2).rename("value"))
    expansion = run(range_expansion, bars, window=2)["value"]
    ranges = np.log(np.array(high) / np.array(low))
    assert expansion.iloc[:2].isna().all()
    assert expansion.iloc[3] == pytest.approx(ranges[3] / ((ranges[1] + ranges[2]) / 2))


def test_consistent_bars_leave_real_bars_unchanged() -> None:
    bars = hand_bars([10.0, 11.0], open_=[10.5, 10.0], high=[11.0, 11.5], low=[9.5, 9.9])
    assert consistent(bars).equals(bars)
    broken = bars.assign(high=[10.0, 11.5])  # a perturbed high below the open
    assert consistent(broken)["high"].tolist() == [10.5, 11.5]
