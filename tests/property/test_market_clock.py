"""ADR 0026: market-clock arithmetic is consistent for arbitrary instants and durations."""

from datetime import date

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.pipeline import REPO
from xq.core.config import load_config
from xq.data.calendar import NAT_NS, MarketClock

CLOCK = MarketClock.for_range(
    load_config("research", config_dir=REPO / "config").sessions_config(),
    date(2024, 3, 1),
    date(2024, 4, 30),
)
FIRST = pd.Timestamp("2024-03-01", tz="UTC").value
LAST = pd.Timestamp("2024-04-10", tz="UTC").value
DAY = pd.Timedelta(days=1).value
instants = st.integers(min_value=FIRST, max_value=LAST)
durations = st.integers(min_value=0, max_value=3 * DAY)


def is_open(t: int) -> bool:
    k = int(np.searchsorted(CLOCK.opens, t, side="right")) - 1
    return k >= 0 and t < CLOCK.closes[k]


@settings(max_examples=300, deadline=None)
@given(t=instants, d1=durations, d2=durations)
def test_advance_adds_exactly_the_market_time(t: int, d1: int, d2: int) -> None:
    one = CLOCK.advance(np.array([t]), d1)[0]
    assert one != NAT_NS
    assert one >= t
    assert is_open(int(one))  # an intended fill time is always an open instant
    assert CLOCK.elapsed(np.array([one]))[0] - CLOCK.elapsed(np.array([t]))[0] == d1
    both = CLOCK.advance(np.array([one]), d2)[0]
    assert both == CLOCK.advance(np.array([t]), d1 + d2)[0]
