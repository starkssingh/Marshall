"""DATA-006: source-clock timestamps to UTC nanoseconds."""

import numpy as np
import pandas as pd
import pytest

from xq.core.errors import ClockConventionError
from xq.core.time import ClockConvention, ClockKind
from xq.data.flags import TickFlag
from xq.data.normalize import canonical_order, normalize_local_times, out_of_order

NY7 = ClockConvention.parse("NY+7")


def naive(*values: str) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([pd.Timestamp(v) for v in values])


def as_utc(result_ns: np.ndarray) -> list[str]:
    return [pd.Timestamp(int(v), unit="ns", tz="UTC").strftime("%Y-%m-%d %H:%M") for v in result_ns]


@pytest.mark.parametrize(
    ("text", "kind", "canonical"),
    [
        ("UTC", ClockKind.UTC, "UTC"),
        ("utc+02:00", ClockKind.FIXED, "UTC+02:00"),
        ("UTC-05:30", ClockKind.FIXED, "UTC-05:30"),
        ("tz:Europe/Athens", ClockKind.IANA, "tz:Europe/Athens"),
        ("NY+7", ClockKind.NY_CLOSE, "NY+7"),
        ("ny-3", ClockKind.NY_CLOSE, "NY-3"),
    ],
)
def test_clock_conventions_parse_and_round_trip(text: str, kind: ClockKind, canonical: str) -> None:
    clock = ClockConvention.parse(text)
    assert clock.kind is kind
    assert str(clock) == canonical
    assert ClockConvention.parse(str(clock)) == clock


@pytest.mark.parametrize("text", ["GMT+2", "UTC+2", "UTC+15:00", "tz:Mars/Base", "NY+13", "server"])
def test_invalid_clock_conventions_are_rejected(text: str) -> None:
    with pytest.raises(ClockConventionError):
        ClockConvention.parse(text)


def test_utc_and_fixed_offsets() -> None:
    local = naive("2024-03-11 12:00")
    assert as_utc(normalize_local_times(local, ClockConvention.parse("UTC")).ts_utc) == [
        "2024-03-11 12:00"
    ]
    fixed = normalize_local_times(local, ClockConvention.parse("UTC+02:00"))
    assert as_utc(fixed.ts_utc) == ["2024-03-11 10:00"]
    assert not fixed.flags.any()


@pytest.mark.parametrize(
    ("server", "utc", "meaning"),
    [
        ("2024-03-08 23:59", "2024-03-08 21:59", "Friday 16:59 EST, before the weekly close"),
        ("2024-03-04 01:00", "2024-03-03 23:00", "Sunday 18:00 EST open, server is UTC+2"),
        ("2024-03-11 01:00", "2024-03-10 22:00", "Sunday 18:00 EDT open, server is UTC+3"),
        ("2024-03-12 00:00", "2024-03-11 21:00", "Monday 17:00 EDT rollover"),
        ("2024-11-05 00:00", "2024-11-04 22:00", "Monday 17:00 EST rollover"),
    ],
)
def test_broker_server_time_follows_us_dst(server: str, utc: str, meaning: str) -> None:
    assert as_utc(normalize_local_times(naive(server), NY7).ts_utc) == [utc], meaning


def test_eet_server_clock_differs_from_ny7_between_us_and_eu_dst_changes() -> None:
    athens = ClockConvention.parse("tz:Europe/Athens")
    # 12 March 2024: US already on DST (server UTC+3), EU not yet (Athens UTC+2).
    mismatch = naive("2024-03-12 12:00")
    assert as_utc(normalize_local_times(mismatch, NY7).ts_utc) == ["2024-03-12 09:00"]
    assert as_utc(normalize_local_times(mismatch, athens).ts_utc) == ["2024-03-12 10:00"]
    # 2 April 2024: both on summer time; the conventions agree again.
    agree = naive("2024-04-02 12:00")
    assert as_utc(normalize_local_times(agree, NY7).ts_utc) == as_utc(
        normalize_local_times(agree, athens).ts_utc
    )


def test_ambiguous_times_resolve_by_file_order_and_are_flagged() -> None:
    # New York repeats 01:00-02:00 on 3 Nov 2024, i.e. server 08:00-09:00. The file's clock jumps
    # back from 08:59 to 08:10 when the repeated hour starts over.
    local = naive(
        "2024-11-03 07:59",
        "2024-11-03 08:30",
        "2024-11-03 08:59",
        "2024-11-03 08:10",
        "2024-11-03 08:40",
        "2024-11-03 09:00",
    )
    result = normalize_local_times(local, NY7)
    assert as_utc(result.ts_utc) == [
        "2024-11-03 04:59",
        "2024-11-03 05:30",
        "2024-11-03 05:59",
        "2024-11-03 06:10",
        "2024-11-03 06:40",
        "2024-11-03 07:00",
    ]
    ambiguous = (result.flags & TickFlag.TS_DST_AMBIGUOUS) != 0
    assert ambiguous.tolist() == [False, True, True, True, True, False]
    assert not ((result.flags & TickFlag.TS_OUT_OF_ORDER) != 0).any()


def test_separate_ambiguous_runs_resolve_independently() -> None:
    local = naive("2023-11-05 08:30", "2023-11-05 08:10", "2024-11-03 08:30")
    result = normalize_local_times(local, NY7)
    # The first run jumps back (DST, then standard); the second never does, so it stays DST.
    assert as_utc(result.ts_utc) == ["2023-11-05 05:30", "2023-11-05 06:10", "2024-11-03 05:30"]


def test_nonexistent_times_shift_forward_and_are_flagged() -> None:
    # New York skips 02:00-03:00 on 10 Mar 2024, i.e. server 09:00-10:00.
    result = normalize_local_times(naive("2024-03-10 09:30", "2024-03-10 10:00"), NY7)
    assert as_utc(result.ts_utc) == ["2024-03-10 07:00", "2024-03-10 07:00"]
    nonexistent = (result.flags & TickFlag.TS_DST_NONEXISTENT) != 0
    assert nonexistent.tolist() == [True, False]


def test_out_of_order_rows_are_flagged_and_sorted_stably() -> None:
    local = naive(
        "2024-03-11 10:00:01", "2024-03-11 10:00:00", "2024-03-11 10:00:01", "2024-03-11 10:00:02"
    )
    result = normalize_local_times(local, ClockConvention.parse("UTC"))
    flagged = (result.flags & TickFlag.TS_OUT_OF_ORDER) != 0
    assert flagged.tolist() == [False, True, False, False]  # equal timestamps are in order
    assert canonical_order(result.ts_utc).tolist() == [1, 0, 2, 3]


def test_out_of_order_on_empty_input() -> None:
    assert out_of_order(np.array([], dtype=np.int64)).tolist() == []


def test_timezone_aware_input_is_rejected() -> None:
    with pytest.raises(ClockConventionError, match="naive"):
        normalize_local_times(pd.DatetimeIndex(["2024-03-11 10:00"], tz="UTC"), NY7)
