"""UTC and trading-day helpers (ARCH-005).

Binding conventions (development plan, section 1):

- timestamps are stored as UTC int64 nanoseconds and handled as tz-aware pandas values; naive
  timestamps are rejected with `NaiveTimestampError`;
- the trading day rolls at 17:00 America/New_York: an instant at or after 17:00 New York belongs to
  the next calendar day's trading day. The roll is a fixed convention, not a configuration option.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.errors import NaiveTimestampError

UTC: Final = "UTC"
NEW_YORK: Final = "America/New_York"
TRADING_DAY_TZ: Final = NEW_YORK
TRADING_DAY_ROLL: Final = time(17, 0)

# Adding this to New York wall-clock time moves 17:00 onto the next midnight.
_ROLL_SHIFT: Final = pd.Timedelta(days=1) - pd.Timedelta(
    hours=TRADING_DAY_ROLL.hour, minutes=TRADING_DAY_ROLL.minute
)

TimestampLike = pd.Timestamp | datetime


def ensure_utc(ts: TimestampLike) -> pd.Timestamp:
    """Return `ts` converted to UTC; raise `NaiveTimestampError` if it has no timezone."""
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        raise NaiveTimestampError(f"naive timestamp {stamp!s}; attach a timezone first")
    return stamp.tz_convert(UTC)


def ensure_utc_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Return `index` converted to UTC with nanosecond resolution; reject naive indexes."""
    if index.tz is None:
        raise NaiveTimestampError("naive DatetimeIndex; attach a timezone first")
    return index.tz_convert(UTC).as_unit("ns")


def to_ns(ts: TimestampLike) -> int:
    """Return `ts` as integer nanoseconds since the Unix epoch (UTC)."""
    return int(ensure_utc(ts).as_unit("ns").value)


def from_ns(ns: int) -> pd.Timestamp:
    """Return a UTC timestamp from integer nanoseconds since the Unix epoch."""
    return pd.Timestamp(int(ns), unit="ns", tz=UTC)


def ns_to_index(values: npt.ArrayLike | Sequence[int]) -> pd.DatetimeIndex:
    """Return a UTC `DatetimeIndex` from integer nanoseconds since the Unix epoch."""
    array = np.asarray(values, dtype=np.int64)
    return pd.DatetimeIndex(pd.to_datetime(array, unit="ns", utc=True)).as_unit("ns")


def utc_now() -> pd.Timestamp:
    """Return the current time as a UTC timestamp."""
    return pd.Timestamp.now(tz=UTC)


def trading_day(ts: TimestampLike) -> date:
    """Return the trading day `ts` belongs to (roll at 17:00 America/New_York)."""
    wall = ensure_utc(ts).tz_convert(TRADING_DAY_TZ).tz_localize(None)
    result: date = (wall + _ROLL_SHIFT).date()
    return result


def trading_days(index: pd.DatetimeIndex) -> npt.NDArray[np.datetime64]:
    """Vectorized `trading_day`: return a ``datetime64[D]`` array aligned with `index`."""
    wall = ensure_utc_index(index).tz_convert(TRADING_DAY_TZ).tz_localize(None)
    shifted = (wall + _ROLL_SHIFT).to_numpy(dtype="datetime64[ns]")
    return shifted.astype("datetime64[D]")


def trading_day_bounds(day: date) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return the half-open UTC interval ``[start, end)`` covered by trading day `day`."""
    start = local_time_to_utc(day - timedelta(days=1), TRADING_DAY_ROLL, TRADING_DAY_TZ)
    end = local_time_to_utc(day, TRADING_DAY_ROLL, TRADING_DAY_TZ)
    return start, end


def local_time_to_utc(day: date, at: time, tz: str) -> pd.Timestamp:
    """Return wall-clock time `at` on `day` in time zone `tz` as a UTC timestamp.

    Raises:
        ValueError: if the local time does not exist or is ambiguous on that date (DST change).
    """
    wall = pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=at.hour, minute=at.minute)
    local = wall.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    if pd.isna(local):
        raise ValueError(f"{wall} is not a unique local time in {tz} (DST change)")
    return local.tz_convert(UTC)
