"""ARCH-005: core enumerations."""

import pandas as pd
import pytest

from xq.core.types import PriceBasis, Side, Timeframe


@pytest.mark.parametrize(
    ("tf", "minutes"),
    [("1m", 1), ("5m", 5), ("15m", 15), ("30m", 30), ("1h", 60), ("4h", 240), ("1d", 1440)],
)
def test_timeframe_durations(tf: str, minutes: int) -> None:
    timeframe = Timeframe(tf)
    assert timeframe.duration == pd.Timedelta(minutes=minutes)
    assert timeframe.nanos == minutes * 60 * 10**9


def test_only_4h_and_daily_bars_anchor_to_the_trading_day() -> None:
    assert {tf for tf in Timeframe if tf.anchored_to_trading_day} == {Timeframe.H4, Timeframe.D1}


def test_sides_fill_on_the_correct_quote() -> None:
    assert Side.BUY.fill_basis is PriceBasis.ASK
    assert Side.SELL.fill_basis is PriceBasis.BID
    assert Side.BUY.sign == 1
    assert Side.SELL.sign == -1
    assert Side.BUY.opposite is Side.SELL
    assert Side.SELL.opposite is Side.BUY
