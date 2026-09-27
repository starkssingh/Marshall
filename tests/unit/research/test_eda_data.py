"""EDA-001 inputs: returns of bars adjacent in market time, and the window check on inputs."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from helpers.datasets import dataset_spec
from helpers.pipeline import REPO, config
from xq.core.types import Timeframe
from xq.data.calendar import MarketClock
from xq.research.eda.data import EdaInputs, bar_returns, bars_per_trading_day, market_clock
from xq.research.reports import DiscoveryWindow, DiscoveryWindowError


def bars(starts: list[str], closes: list[float], tf: Timeframe) -> pd.DataFrame:
    start = pd.to_datetime(starts, utc=True)
    return pd.DataFrame(
        {
            "bar_start_utc": start,
            "available_at_utc": start + tf.duration,
            "close": closes,
            "trading_day": [date(2024, 3, 1)] * len(starts),
            "tick_count": np.arange(len(starts), dtype=np.int64) + 10,
            "spread_mean": np.full(len(starts), 0.2),
        }
    )


def clock() -> MarketClock:
    return market_clock(
        config(REPO).sessions_config(),
        pd.Timestamp("2024-03-08", tz="UTC"),
        pd.Timestamp("2024-03-20", tz="UTC"),
    )


def test_returns_cross_the_daily_break_but_not_a_missing_bar() -> None:
    # Tuesday 2024-03-12 (EDT): 15:00 and 16:00 New York, the reopen at 18:00, 19:00, then 20:00
    # missing and 21:00 present.
    frame = bars(
        [
            "2024-03-12T19:00:00",
            "2024-03-12T20:00:00",
            "2024-03-12T22:00:00",
            "2024-03-12T23:00:00",
            "2024-03-13T01:00:00",
        ],
        [100.0, 101.0, 102.0, 101.0, 103.0],
        Timeframe.H1,
    )
    out = bar_returns(frame, Timeframe.H1, clock())
    assert out["bar_start"].tolist() == list(
        pd.to_datetime(
            ["2024-03-12T20:00:00", "2024-03-12T22:00:00", "2024-03-12T23:00:00"], utc=True
        )
    )
    np.testing.assert_allclose(out["ret"], np.log([101 / 100, 102 / 101, 101 / 102]))
    across = out.iloc[1]
    assert across["ret_start"] == pd.Timestamp("2024-03-12T21:00:00Z")  # the 17:00 close
    assert across["ret_end"] == pd.Timestamp("2024-03-12T23:00:00Z")
    assert across["price"] == 101.0
    np.testing.assert_allclose(out["spread_bps"], 0.2 / np.array([101.0, 102.0, 101.0]) * 1e4)
    assert out["tick_count"].tolist() == [11, 12, 13]


def test_daily_returns_cross_the_weekend_but_not_a_missing_day() -> None:
    # Daily bars start at 17:00 New York the evening before: Thursday 14th (trading day Friday
    # 15th), Sunday 17th (Monday 18th), then Tuesday 19th missing, Wednesday 20th present.
    frame = bars(
        [
            "2024-03-13T21:00:00",
            "2024-03-14T21:00:00",
            "2024-03-17T21:00:00",
            "2024-03-19T21:00:00",
        ],
        [100.0, 102.0, 101.0, 105.0],
        Timeframe.D1,
    )
    out = bar_returns(frame, Timeframe.D1, clock())
    assert len(out) == 2
    np.testing.assert_allclose(out["ret"], np.log([102 / 100, 101 / 102]))
    assert out["ret_start"].iloc[1] == pd.Timestamp("2024-03-15T21:00:00Z")


def test_too_few_bars_give_no_returns() -> None:
    out = bar_returns(bars(["2024-03-12T19:00:00"], [1.0], Timeframe.H1), Timeframe.H1, clock())
    assert out.empty
    assert list(out.columns) == [
        "bar_start",
        "ret_start",
        "ret_end",
        "trading_day",
        "price",
        "close",
        "ret",
        "tick_count",
        "spread_bps",
    ]


def test_bars_per_trading_day() -> None:
    sessions = config(REPO).sessions_config()
    assert bars_per_trading_day(Timeframe.M1, sessions) == 23 * 60
    assert bars_per_trading_day(Timeframe.M15, sessions) == 92
    assert bars_per_trading_day(Timeframe.H4, sessions) == 6
    assert bars_per_trading_day(Timeframe.D1, sessions) == 1


def test_inputs_refuse_bars_available_after_the_window() -> None:
    window = DiscoveryWindow(
        pd.Timestamp("2024-03-12T00:00:00Z"), pd.Timestamp("2024-03-12T21:00:00Z"), "test"
    )
    inside = bars(["2024-03-12T19:00:00", "2024-03-12T20:00:00"], [1.0, 1.0], Timeframe.H1)
    EdaInputs("ds-x", dataset_spec(), window, {Timeframe.H1: inside}, frozenset(), clock())
    late = bars(["2024-03-12T20:00:00", "2024-03-12T20:30:00"], [1.0, 1.0], Timeframe.H1)
    with pytest.raises(DiscoveryWindowError, match="post-discovery"):
        EdaInputs("ds-x", dataset_spec(), window, {Timeframe.H1: late}, frozenset(), clock())
