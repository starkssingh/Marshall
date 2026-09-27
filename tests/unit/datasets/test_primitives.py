"""DS-003: causal primitives on hand-computed cases."""

import numpy as np
import pandas as pd
import pytest

from xq.core.errors import NaiveTimestampError
from xq.datasets import primitives as p

T0 = pd.Timestamp("2024-03-12 10:00", tz="UTC")


def series(values: list[float], minutes: list[int] | None = None) -> pd.Series:
    offsets = minutes if minutes is not None else list(range(len(values)))
    index = pd.DatetimeIndex([T0 + pd.Timedelta(minutes=m) for m in offsets])
    return pd.Series(values, index=index, dtype=np.float64)


def test_row_window_is_trailing() -> None:
    s = series([1, 2, 3, 4, 5])
    np.testing.assert_allclose(p.rolling(s, 3), [np.nan, np.nan, 2, 3, 4])
    np.testing.assert_allclose(p.rolling(s, 3, min_periods=1), [1, 1.5, 2, 3, 4])
    np.testing.assert_allclose(p.rolling(s, 2, "max"), [np.nan, 2, 3, 4, 5])


def test_time_window_covers_the_half_open_trailing_span() -> None:
    s = series([1, 2, 4, 8], minutes=[0, 10, 30, 31])
    # (t - 30min, t]: at 10:30 the 10:00 row is excluded, at 10:31 too.
    np.testing.assert_allclose(p.rolling(s, "30min", "sum"), [1, 3, 6, 14])


def test_expanding_and_ewm() -> None:
    s = series([2, 4, 6])
    np.testing.assert_allclose(p.expanding(s), [2, 3, 4])
    # adjust=True weights (1-a)^k with a = 2/(span+1) = 0.5 for span 3.
    np.testing.assert_allclose(
        p.ewm_mean(s, span=3), [2, (4 + 0.5 * 2) / 1.5, (6 + 2 + 0.5) / 1.75]
    )


def test_returns() -> None:
    prices = series([100, 110, 121])
    np.testing.assert_allclose(p.log_returns(prices), [np.nan, np.log(1.1), np.log(1.1)])
    np.testing.assert_allclose(p.log_returns(prices, 2), [np.nan, np.nan, np.log(1.21)])
    np.testing.assert_allclose(p.simple_returns(prices), [np.nan, 0.1, 0.1])


def test_volatility_estimates() -> None:
    returns = series([0.01, -0.02, 0.02, np.nan, 0.01])
    np.testing.assert_allclose(
        p.realized_volatility(returns, 2, min_periods=1),
        [0.01, np.sqrt(0.0005), np.sqrt(0.0008), 0.02, 0.01],
    )
    ewma = p.ewma_volatility(returns, span=3)
    assert ewma.iloc[0] == pytest.approx(0.01)
    assert ewma.iloc[1] == pytest.approx(np.sqrt((0.0004 + 0.5 * 0.0001) / 1.5))
    assert ewma.iloc[3] == pytest.approx(ewma.iloc[2])  # a missing return adds no information


def test_vol_normalized_uses_the_scale_known_before_the_return() -> None:
    returns = series([0.01, 0.04, -0.02])
    sigma = series([0.01, 0.02, 0.0])
    np.testing.assert_allclose(p.vol_normalized(returns, sigma), [np.nan, 4.0, -1.0])
    np.testing.assert_allclose(p.vol_normalized(returns, sigma, lag=0), [1.0, 2.0, np.nan])
    with pytest.raises(ValueError, match="same index"):
        p.vol_normalized(returns, sigma.iloc[:2])


def test_resample_labels_bins_by_their_end() -> None:
    s = series([1, 2, 3, 4], minutes=[0, 14, 15, 44])
    out = p.resample_causal(s, "15min", "last", latency=pd.Timedelta(0))
    assert out.index.tolist() == [
        T0 + pd.Timedelta(minutes=15),
        T0 + pd.Timedelta(minutes=30),
        T0 + pd.Timedelta(minutes=45),
    ]
    assert out.tolist() == [2, 3, 4]
    assert p.resample_causal(s, "15min", "sum", latency=pd.Timedelta(0)).tolist() == [3, 3, 4]


def test_resample_labels_bins_when_their_last_member_is_available() -> None:
    # Observations become available 2 minutes after their index time: the bin [0, 15) holds the
    # 14-minute observation, known only at 16 minutes.
    s = series([1, 2, 3, 4], minutes=[0, 14, 15, 44])
    out = p.resample_causal(s, "15min", "last", latency=pd.Timedelta(minutes=2))
    assert out.index.tolist() == [
        T0 + pd.Timedelta(minutes=17),
        T0 + pd.Timedelta(minutes=32),
        T0 + pd.Timedelta(minutes=47),
    ]
    assert out.tolist() == [2, 3, 4]
    with pytest.raises(ValueError, match="non-negative"):
        p.resample_causal(s, "15min", "last", latency=pd.Timedelta(minutes=-1))


def test_lag_and_no_negative_shifts() -> None:
    s = series([1, 2, 3])
    np.testing.assert_allclose(p.lag(s), [np.nan, 1, 2])
    for call in (
        lambda: p.lag(s, -1),
        lambda: p.log_returns(s, 0),
        lambda: p.simple_returns(s, -2),
    ):
        with pytest.raises(ValueError, match="no look-ahead"):
            call()


def test_order_and_timezone_are_enforced() -> None:
    naive = pd.Series([1.0, 2.0], index=pd.DatetimeIndex(["2024-03-12 10:00", "2024-03-12 10:01"]))
    with pytest.raises(NaiveTimestampError):
        p.rolling(naive, 2)
    backwards = series([1, 2], minutes=[5, 0])
    with pytest.raises(ValueError, match="increasing time order"):
        p.ewm_mean(backwards, span=2)
    with pytest.raises(TypeError, match="DatetimeIndex"):
        p.rolling(pd.Series([1.0, 2.0]), "1h")
    with pytest.raises(ValueError, match="positive"):
        p.rolling(series([1, 2]), 0)
