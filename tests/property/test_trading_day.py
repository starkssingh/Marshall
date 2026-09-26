"""ARCH-005: every instant lies inside the bounds of its own trading day."""

from datetime import timedelta

import pandas as pd
from hypothesis import given
from hypothesis import strategies as st

from xq.core.time import trading_day, trading_day_bounds, trading_days

# 2000-01-01 .. 2040-01-01 in nanoseconds since the epoch.
INSTANTS = st.integers(min_value=946_684_800 * 10**9, max_value=2_208_988_800 * 10**9)


@given(INSTANTS)
def test_instant_lies_within_its_trading_day(ns: int) -> None:
    ts = pd.Timestamp(ns, unit="ns", tz="UTC")
    day = trading_day(ts)
    start, end = trading_day_bounds(day)
    assert start <= ts < end
    # Trading days are 23, 24 or 25 hours long, depending on DST changes inside them.
    assert timedelta(hours=23) <= end - start <= timedelta(hours=25)


@given(st.lists(INSTANTS, min_size=1, max_size=50))
def test_vectorized_trading_days_match_scalar(values: list[int]) -> None:
    index = pd.DatetimeIndex(pd.to_datetime(values, unit="ns", utc=True))
    vectorized = trading_days(index)
    assert [d.item() for d in vectorized] == [trading_day(ts) for ts in index]
