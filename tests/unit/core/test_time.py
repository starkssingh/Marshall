"""ARCH-005: UTC conversion and the 17:00 New York trading-day roll."""

from datetime import UTC, date, datetime, time

import numpy as np
import pandas as pd
import pytest

from xq.core.errors import NaiveTimestampError
from xq.core.time import (
    ensure_utc,
    ensure_utc_index,
    from_ns,
    local_time_to_utc,
    ns_to_index,
    to_ns,
    trading_day,
    trading_day_bounds,
    trading_days,
)

# US DST in 2024 started Sunday 10 March and ended Sunday 3 November. Each case gives a UTC instant,
# the New York wall time it corresponds to, and the trading day it must fall in.
ROLL_CASES = [
    # Friday before DST starts: EST, 17:00 New York = 22:00 UTC.
    ("2024-03-08 21:59:00", "16:59 EST", date(2024, 3, 8)),
    ("2024-03-08 22:00:00", "17:00 EST", date(2024, 3, 9)),
    ("2024-03-08 22:01:00", "17:01 EST", date(2024, 3, 9)),
    # Monday after DST starts: EDT, 17:00 New York = 21:00 UTC.
    ("2024-03-11 20:59:00", "16:59 EDT", date(2024, 3, 11)),
    ("2024-03-11 21:00:00", "17:00 EDT", date(2024, 3, 12)),
    ("2024-03-11 21:01:00", "17:01 EDT", date(2024, 3, 12)),
    # Friday before DST ends: EDT.
    ("2024-11-01 20:59:00", "16:59 EDT", date(2024, 11, 1)),
    ("2024-11-01 21:00:00", "17:00 EDT", date(2024, 11, 2)),
    ("2024-11-01 21:01:00", "17:01 EDT", date(2024, 11, 2)),
    # Monday after DST ends: EST.
    ("2024-11-04 21:59:00", "16:59 EST", date(2024, 11, 4)),
    ("2024-11-04 22:00:00", "17:00 EST", date(2024, 11, 5)),
    ("2024-11-04 22:01:00", "17:01 EST", date(2024, 11, 5)),
    # Sunday weekly open (17:00 New York) starts Monday's trading day, on both sides of DST.
    ("2024-03-10 21:00:00", "17:00 EDT", date(2024, 3, 11)),
    ("2024-11-03 22:00:00", "17:00 EST", date(2024, 11, 4)),
]


@pytest.mark.parametrize(("utc", "new_york", "expected"), ROLL_CASES)
def test_trading_day_roll_across_dst(utc: str, new_york: str, expected: date) -> None:
    ts = pd.Timestamp(utc, tz="UTC")
    wall, abbreviation = new_york.split()
    local = ts.tz_convert("America/New_York")
    assert local.strftime("%H:%M") == wall
    assert local.tzname() == abbreviation
    assert trading_day(ts) == expected


def test_trading_days_matches_scalar_version() -> None:
    index = pd.DatetimeIndex([pd.Timestamp(utc, tz="UTC") for utc, _, _ in ROLL_CASES])
    expected = np.array([d for _, _, d in ROLL_CASES], dtype="datetime64[D]")
    np.testing.assert_array_equal(trading_days(index), expected)


def test_trading_day_accepts_any_timezone() -> None:
    tokyo = pd.Timestamp("2024-03-12 06:00", tz="Asia/Tokyo")  # 2024-03-11 21:00 UTC = 17:00 EDT
    assert trading_day(tokyo) == date(2024, 3, 12)
    assert trading_day(datetime(2024, 3, 11, 20, 59, tzinfo=UTC)) == date(2024, 3, 11)


@pytest.mark.parametrize(
    ("day", "start_utc", "end_utc"),
    [
        (date(2024, 3, 8), "2024-03-07 22:00", "2024-03-08 22:00"),
        (date(2024, 3, 11), "2024-03-10 21:00", "2024-03-11 21:00"),
        (date(2024, 11, 4), "2024-11-03 22:00", "2024-11-04 22:00"),
    ],
)
def test_trading_day_bounds(day: date, start_utc: str, end_utc: str) -> None:
    start, end = trading_day_bounds(day)
    assert start == pd.Timestamp(start_utc, tz="UTC")
    assert end == pd.Timestamp(end_utc, tz="UTC")
    assert trading_day(start) == day
    assert trading_day(end - pd.Timedelta(1, unit="ns")) == day
    assert trading_day(end) != day


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(NaiveTimestampError):
        trading_day(pd.Timestamp("2024-03-11 21:00"))
    with pytest.raises(NaiveTimestampError):
        ensure_utc(datetime(2024, 3, 11, 21))  # noqa: DTZ001 - deliberately naive
    with pytest.raises(NaiveTimestampError):
        ensure_utc_index(pd.DatetimeIndex(["2024-03-11 21:00"]))


def test_nanosecond_round_trip() -> None:
    ts = pd.Timestamp("2024-03-11 21:00:00.123456789", tz="UTC")
    ns = to_ns(ts)
    assert ns == 1710190800123456789
    assert from_ns(ns) == ts
    index = ns_to_index([ns, ns + 1])
    assert index.tz is not None
    assert str(index.dtype) == "datetime64[ns, UTC]"
    assert index[1] - index[0] == pd.Timedelta(1, unit="ns")


def test_to_ns_converts_from_other_zones() -> None:
    new_york = pd.Timestamp("2024-03-11 17:00", tz="America/New_York")
    assert to_ns(new_york) == to_ns(pd.Timestamp("2024-03-11 21:00", tz="UTC"))


def test_local_time_to_utc_handles_dst_per_date() -> None:
    assert local_time_to_utc(date(2024, 3, 29), time(8, 0), "Europe/London") == pd.Timestamp(
        "2024-03-29 08:00", tz="UTC"
    )
    assert local_time_to_utc(date(2024, 4, 2), time(8, 0), "Europe/London") == pd.Timestamp(
        "2024-04-02 07:00", tz="UTC"
    )


@pytest.mark.parametrize(
    ("day", "at"),
    [
        (date(2024, 3, 10), time(2, 30)),  # does not exist in New York
        (date(2024, 11, 3), time(1, 30)),  # happens twice in New York
    ],
)
def test_local_time_to_utc_rejects_non_unique_times(day: date, at: time) -> None:
    with pytest.raises(ValueError, match="not a unique local time"):
        local_time_to_utc(day, at, "America/New_York")
