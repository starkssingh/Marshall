"""DATA-006: UTC -> broker server time -> UTC is the identity away from DST transitions."""

import pandas as pd
from hypothesis import assume, given
from hypothesis import strategies as st

from xq.core.time import ClockConvention
from xq.data.normalize import normalize_local_times

INSTANTS = st.integers(min_value=946_684_800 * 10**9, max_value=2_208_988_800 * 10**9)


@given(INSTANTS, st.sampled_from(["NY+7", "tz:Europe/Athens", "UTC+02:00", "UTC"]))
def test_round_trip_through_source_clock(ns: int, clock_text: str) -> None:
    clock = ClockConvention.parse(clock_text)
    instant = pd.Timestamp(ns, unit="ns", tz="UTC")
    if clock.tz is not None:
        local = instant.tz_convert(clock.tz).tz_localize(None) + pd.Timedelta(
            hours=clock.shift_hours
        )
    else:
        local = instant.tz_localize(None) + pd.Timedelta(minutes=clock.offset_minutes)
    result = normalize_local_times(pd.DatetimeIndex([local]), clock)
    assume(result.flags[0] == 0)  # a lone repeated wall time cannot be disambiguated
    assert int(result.ts_utc[0]) == ns
