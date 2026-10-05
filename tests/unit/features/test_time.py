"""FEAT-006: time and event-proximity features match the session table, across DST changes."""

import numpy as np
import pandas as pd
import pytest

from helpers.features import SESSIONS, hand_bars, run
from xq.core.errors import ConfigError
from xq.core.time import trading_day
from xq.core.types import Timeframe
from xq.data.sessions import build_session_table
from xq.features.time import day_of_week, event_minutes, session, time_of_day


def decisions_bars(starts: list[str], timeframe: Timeframe = Timeframe.M15) -> pd.DataFrame:
    return hand_bars([2000.0] * len(starts), starts=starts, timeframe=timeframe)


def test_time_of_day_is_the_local_clock_on_a_circle() -> None:
    # bar 12:45-13:00 UTC on 2024-03-12: decided at 13:00 UTC, 09:00 in New York (EDT)
    out = run(time_of_day, decisions_bars(["2024-03-12 12:45"]), tz="America/New_York")
    angle = 2 * np.pi * 9 * 60 / 1440
    assert out.iloc[0].tolist() == pytest.approx([np.sin(angle), np.cos(angle)])
    # 23:59 and 00:00 are neighbours
    late = run(time_of_day, decisions_bars(["2024-03-13 03:44"]), tz="America/New_York")
    early = run(time_of_day, decisions_bars(["2024-03-13 03:45"]), tz="America/New_York")
    assert np.hypot(*(late.iloc[0] - early.iloc[0])) < 0.01
    with pytest.raises(ConfigError):
        time_of_day.validate({"tz": "Mars/Olympus"})


def test_day_of_week_is_the_trading_day_s() -> None:
    # decided 13:00 UTC Tuesday, and 21:15 UTC Tuesday (after the 17:00 New York roll: Wednesday)
    out = run(day_of_week, decisions_bars(["2024-03-12 12:45", "2024-03-12 21:00"]))
    assert out.iloc[0].tolist() == [0.0, 1.0, 0.0, 0.0, 0.0]
    assert out.iloc[1].tolist() == [0.0, 0.0, 1.0, 0.0, 0.0]


def test_session_flags_match_the_session_table_across_dst() -> None:
    # 15-minute decisions over three days: both zones in winter, New York only in summer time,
    # both in summer time
    starts = [
        str(t)
        for day in ("2024-03-05", "2024-03-12", "2024-04-02")
        for t in pd.date_range(f"{day} 00:00", periods=96, freq="15min")
    ]
    bars = decisions_bars(starts)
    out = run(session, bars)
    assert list(out.columns) == ["tokyo", "london", "new_york", "london_new_york"]
    t = pd.DatetimeIndex(bars["available_at_utc"])
    days = [trading_day(x) for x in t]
    table = build_session_table(SESSIONS, min(days), max(days)).set_index("trading_day")
    for name in out.columns:
        rows = table.reindex(days)
        opens = pd.to_datetime(rows[f"{name}_open_utc"], utc=True).to_numpy()
        closes = pd.to_datetime(rows[f"{name}_close_utc"], utc=True).to_numpy()
        expected = (opens <= t.to_numpy()) & (t.to_numpy() < closes)
        np.testing.assert_array_equal(out[name].to_numpy(dtype=bool), expected)
    # London opens at 08:00 UTC in March (GMT) and at 07:00 UTC in April (BST)
    london = out["london"].set_axis(t)
    assert london.loc["2024-03-12 08:00"] == 1.0
    assert london.loc["2024-03-12 07:45"] == 0.0
    assert london.loc["2024-04-02 07:00"] == 1.0


def test_minutes_to_and_since_events() -> None:
    bars = decisions_bars(["2024-03-12 12:45", "2024-03-15 20:45"])  # Tue 13:00, Fri 21:00 UTC
    release = run(event_minutes, bars, anchor="us_data_release", cap_minutes=1440)
    # Tuesday 09:00 New York: the 08:30 release was 30 minutes ago, the next is in 23.5 hours
    assert release.iloc[0].tolist() == [1410.0, 30.0]
    # Friday 17:00 New York: the next release is on Monday, beyond the cap
    assert release.iloc[1].tolist() == [1440.0, 510.0]
    rollover = run(event_minutes, bars, anchor="rollover", cap_minutes=1440)
    assert rollover.iloc[0].tolist() == [480.0, 960.0]  # 17:00 New York, today and yesterday
    lbma = run(event_minutes, bars, anchor="lbma_am", cap_minutes=1440)
    assert lbma.iloc[0].tolist() == [1290.0, 150.0]  # 10:30 London (GMT) = 10:30 UTC
    with pytest.raises(ConfigError, match="no event anchor"):
        run(event_minutes, bars, anchor="fomc", cap_minutes=60)
