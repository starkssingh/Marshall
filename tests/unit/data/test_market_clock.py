"""ADR 0026: trading time on the market clock skips the daily break, weekends and holidays."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import load_config
from xq.data.calendar import NAT_NS, MarketClock

SESSIONS = load_config("research", config_dir=REPO / "config").sessions_config()
CLOCK = MarketClock.for_range(SESSIONS, date(2024, 1, 1), date(2024, 4, 30))
H = pd.Timedelta(hours=1)
S = pd.Timedelta(seconds=1)


def ns(*times: str) -> np.ndarray:
    return np.array([pd.Timestamp(t, tz="UTC").value for t in times], dtype=np.int64)


def advance(when: str, duration: pd.Timedelta) -> pd.Timestamp:
    return pd.Timestamp(int(CLOCK.advance(ns(when), duration.value)[0]), tz="UTC")


@pytest.mark.parametrize(
    ("when", "duration", "expected"),
    [
        # Tuesday 12 March 2024 (EDT): the market is open 22:00 UTC Monday to 21:00 UTC Tuesday.
        ("2024-03-12 12:00", pd.Timedelta(0), "2024-03-12 12:00"),
        ("2024-03-12 12:00", H, "2024-03-12 13:00"),
        ("2024-03-12 20:00", H, "2024-03-12 22:00"),  # lands on the close: the reopen
        ("2024-03-12 20:00", H + S, "2024-03-12 22:00:01"),  # the daily break is skipped
        ("2024-03-12 21:00", pd.Timedelta(0), "2024-03-12 22:00"),  # at the close: the reopen
        ("2024-03-12 21:30", S, "2024-03-12 22:00:01"),  # decided in the break
        # Friday 16:00 EDT plus one hour of the Monday trading day (Sunday 18:00 EDT open).
        ("2024-03-15 20:00", 2 * H, "2024-03-17 23:00"),
        # Across the US DST change: Friday 8 March closes 17:00 EST, Sunday 10 March opens EDT.
        ("2024-03-08 21:00", 2 * H, "2024-03-10 23:00"),
        # Good Friday (29 March) is closed: Thursday's close to Sunday's open.
        ("2024-03-28 20:00", 2 * H, "2024-03-31 23:00"),
        # Martin Luther King Jr. Day closes early at 14:30 EST (from 2022, ADR 0070) and reopens
        # at 18:00 EST.
        ("2024-01-15 19:00", H, "2024-01-15 23:30"),
    ],
)
def test_advance_counts_only_market_open_time(
    when: str, duration: pd.Timedelta, expected: str
) -> None:
    assert advance(when, duration) == pd.Timestamp(expected, tz="UTC")


def test_one_day_of_market_time_is_one_trading_day_plus_an_hour() -> None:
    # A trading day has 23 market hours, so a 24-hour horizon from Tuesday 12:00 UTC ends at
    # Wednesday 13:00 UTC, and from Friday 16:00 New York it runs past Monday's close.
    assert advance("2024-03-12 12:00", pd.Timedelta("1d")) == pd.Timestamp(
        "2024-03-13 13:00", tz="UTC"
    )
    assert advance("2024-03-15 20:00", pd.Timedelta("1d")) == pd.Timestamp(
        "2024-03-18 22:00", tz="UTC"
    )


def test_elapsed_ignores_closed_time() -> None:
    elapsed = CLOCK.elapsed(ns("2024-03-12 20:00", "2024-03-12 21:30", "2024-03-12 22:30"))
    assert elapsed[1] - elapsed[0] == H.value
    assert elapsed[2] - elapsed[1] == pd.Timedelta(minutes=30).value


def test_crosses_close_when_a_close_lies_in_the_holding_period() -> None:
    starts = ns("2024-03-12 20:00", "2024-03-12 12:00", "2024-03-12 21:00", "2024-03-12 20:00")
    ends = ns("2024-03-12 22:00:01", "2024-03-12 13:00", "2024-03-12 22:30", "2024-03-12 21:00")
    # held over the break; intraday; entered at the close instant; exited at the close instant
    assert CLOCK.crosses_close(starts, ends).tolist() == [True, False, True, False]


def test_beyond_the_covered_range() -> None:
    assert CLOCK.advance(ns("2024-04-30 12:00"), pd.Timedelta("2d").value)[0] == NAT_NS
    with pytest.raises(ValueError, match="outside the market clock"):
        CLOCK.advance(ns("2024-06-01 12:00"), 0)
    with pytest.raises(ValueError, match="non-negative"):
        CLOCK.advance(ns("2024-03-12 12:00"), -1)


def test_intervals_are_validated() -> None:
    one = np.array([10], dtype=np.int64)
    with pytest.raises(ValueError, match="open before each close"):
        MarketClock(one, one, 0, 100)
    with pytest.raises(ValueError, match="must not overlap"):
        MarketClock(np.array([0, 5], dtype=np.int64), np.array([10, 20], dtype=np.int64), 0, 100)


def test_is_open_marks_the_market_open_intervals() -> None:
    times = [
        "2024-03-12 20:59:59",  # Tuesday 16:59:59 EDT: open
        "2024-03-12 21:00",  # the 17:00 close itself: closed
        "2024-03-12 21:30",  # the daily break
        "2024-03-12 22:00",  # the 18:00 reopen: open
        "2024-03-16 12:00",  # Saturday
        "2024-03-17 22:00",  # Sunday 18:00 EDT open
        "2024-03-29 12:00",  # Good Friday: closed all day
    ]
    stamps = pd.DatetimeIndex([pd.Timestamp(t, tz="UTC") for t in times])
    values = stamps.as_unit("ns").to_numpy("datetime64[ns]").view("int64")
    np.testing.assert_array_equal(
        CLOCK.is_open(values), [True, False, False, True, False, True, False]
    )
