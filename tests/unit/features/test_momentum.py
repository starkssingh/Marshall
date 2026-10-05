"""FEAT-003: momentum features against hand computations (RSI and MACD step by step, the slope
t-statistic against SciPy's regression)."""

import numpy as np
import pytest
from scipy.stats import linregress

from helpers.features import hand_bars, run
from xq.core.errors import ConfigError
from xq.features.base import bar_sigma
from xq.features.momentum import ma_slope_t, macd_hist, roc, rsi, sign_agreement


def test_rate_of_change() -> None:
    out = run(roc, hand_bars([100.0, 105.0, 110.25]), bars=2)["value"]
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(0.1025)


def test_wilder_rsi_by_hand() -> None:
    # changes +1, -0.5, +1.5, -1, +0.5; window 3
    close = [10.0, 11.0, 10.5, 12.0, 11.0, 11.5]
    out = run(rsi, hand_bars(close), window=3)["value"]
    gain, loss = (1 + 0 + 1.5) / 3, (0 + 0.5 + 0) / 3  # first averages: simple means
    expected = [100 - 100 / (1 + gain / loss)]  # 83.33
    gain, loss = (gain * 2 + 0) / 3, (loss * 2 + 1.0) / 3  # Wilder: -1
    expected.append(100 - 100 / (1 + gain / loss))  # 55.56
    gain, loss = (gain * 2 + 0.5) / 3, (loss * 2 + 0) / 3  # Wilder: +0.5
    expected.append(100 - 100 / (1 + gain / loss))  # 64.44
    assert out.iloc[:3].isna().all()
    assert out.iloc[3:].tolist() == pytest.approx(expected)
    assert expected == pytest.approx([83.3333, 55.5556, 64.4444], abs=1e-4)
    # only gains: 100; no movement: 50
    assert run(rsi, hand_bars([1.0, 2.0, 3.0, 4.0]), window=3)["value"].iloc[3] == 100.0
    assert run(rsi, hand_bars([2.0, 2.0, 2.0, 2.0]), window=3)["value"].iloc[3] == 50.0


def test_macd_histogram_by_hand() -> None:
    close = [10.0, 11.0, 12.0, 11.0, 13.0, 14.0]
    out = run(macd_hist, hand_bars(close), fast=2, slow=3, signal=2, sigma_span=2)["value"]

    def ema(values: list[float], span: int) -> list[float]:
        a, out_ = 2 / (span + 1), [values[0]]
        for x in values[1:]:
            out_.append(a * x + (1 - a) * out_[-1])
        return out_

    fast, slow = ema(close, 2), ema(close, 3)
    macd = [f - s for f, s in zip(fast[2:], slow[2:], strict=True)]  # known from 3 closes
    signal = ema(macd, 2)
    histogram = [m - s for m, s in zip(macd[1:], signal[1:], strict=True)]  # from 2 MACDs
    assert macd[:2] == pytest.approx([11.5556 - 11.25, 11.1852 - 11.125], abs=1e-4)
    sigma = bar_sigma(hand_bars(close), 2).to_numpy()
    assert out.iloc[:3].isna().all()
    scaled = out.iloc[3:].to_numpy() * np.array(close[3:]) * sigma[3:]
    assert scaled == pytest.approx(histogram)
    with pytest.raises(ConfigError, match="slow must be longer"):
        macd_hist.validate({"fast": 26, "slow": 12, "signal": 9, "sigma_span": 96})


def test_slope_t_statistic_matches_a_least_squares_regression() -> None:
    rng = np.random.default_rng(3)
    close = 2000 * np.exp(np.cumsum(rng.normal(0.0005, 0.002, 40)))
    out = run(ma_slope_t, hand_bars(list(close)), window=10)["value"]
    assert out.iloc[:9].isna().all()
    for end in (9, 25, 39):
        y = np.log(close[end - 9 : end + 1])
        fit = linregress(np.arange(10), y)
        assert out.iloc[end] == pytest.approx(fit.slope / fit.stderr, rel=1e-9)
    straight = run(ma_slope_t, hand_bars([1.0, 2.0, 4.0, 8.0]), window=3)["value"]
    assert straight.isna().all()  # a perfect exponential trend has no residual noise


def test_sign_agreement_across_horizons() -> None:
    close = [10.0, 11.0, 12.0, 11.5, 13.0]
    out = run(sign_agreement, hand_bars(close), horizons=[1, 2, 4])["value"]
    assert out.iloc[:4].isna().all()
    assert out.iloc[4] == pytest.approx(1.0)  # up over 1, 2 and 4 bars
    down = run(sign_agreement, hand_bars([10.0, 12.0, 11.0]), horizons=[1, 2])["value"]
    assert down.iloc[2] == pytest.approx(0.0)  # down over 1 bar, up over 2
    with pytest.raises(ConfigError):
        sign_agreement.validate({"horizons": [1, 1]})
