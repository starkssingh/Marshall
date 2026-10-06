"""DS-007: calendar and session columns known in advance."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import SessionsConfig, load_config
from xq.core.time import trading_day
from xq.data.sessions import build_session_table
from xq.datasets.calendar_columns import calendar_columns


@pytest.fixture(scope="module")
def sessions() -> SessionsConfig:
    return load_config("research", config_dir=REPO / "config").sessions_config()


def at(sessions: SessionsConfig, *times: str) -> pd.DataFrame:
    return calendar_columns(pd.DatetimeIndex(pd.to_datetime(list(times), utc=True)), sessions)


def test_hand_computed_tuesday_after_us_dst(sessions: SessionsConfig) -> None:
    # 2024-03-12: New York on EDT (UTC-4), London still on GMT (UTC+0).
    row = at(sessions, "2024-03-12 13:15").iloc[0]
    assert row["trading_day"] == date(2024, 3, 12)
    assert row["day_of_week"] == 1
    assert row["is_open"]
    assert not row["is_early_close"]
    assert row["minutes_to_market_close"] == 465  # close 17:00 EDT = 21:00 UTC
    assert not row["in_tokyo"]
    assert np.isnan(row["tokyo_minutes_since_open"])
    assert row["in_london"]
    assert row["london_minutes_since_open"] == 315  # open 08:00 GMT
    assert row["in_new_york"]
    assert row["new_york_minutes_since_open"] == 75  # open 08:00 EDT = 12:00 UTC
    assert row["in_london_new_york"]
    assert row["london_new_york_minutes_since_open"] == 75
    assert row["minutes_since_lbma_am"] == 165
    assert row["minutes_to_lbma_am"] == 1275  # tomorrow's 10:30 London
    assert row["minutes_to_lbma_pm"] == 105
    assert row["minutes_since_comex_open"] == 55
    assert row["minutes_since_us_data_release"] == 45
    assert not row["in_us_data_release_window"]
    assert row["minutes_to_rollover"] == 465
    assert row["minutes_since_rollover"] == 975  # 21:00 UTC the day before


def test_tokyo_session_early_in_the_trading_day(sessions: SessionsConfig) -> None:
    row = at(sessions, "2024-03-12 03:00").iloc[0]
    assert row["in_tokyo"]
    assert row["tokyo_minutes_since_open"] == 180  # 09:00 JST = 00:00 UTC
    assert not row["in_london"]


def test_uk_summer_time_moves_london(sessions: SessionsConfig) -> None:
    before, after = at(sessions, "2024-03-28 07:30", "2024-04-02 07:30").itertuples()
    assert not before.in_london  # GMT: London opens 08:00 UTC
    assert after.in_london  # BST: London opens 07:00 UTC
    assert after.london_minutes_since_open == 30


def test_release_and_rollover_windows(sessions: SessionsConfig) -> None:
    flags = at(
        sessions,
        "2024-03-12 12:24",
        "2024-03-12 12:25",
        "2024-03-12 12:59",
        "2024-03-12 13:00",
        "2024-03-12 20:44",  # 16:44 EDT
        "2024-03-12 20:45",  # 16:45 EDT: the pre-close
        "2024-03-12 21:30",  # the daily break
        "2024-03-12 22:14",  # 18:14 EDT: just after the reopen
        "2024-03-12 22:15",
    )
    assert flags["in_us_data_release_window"].tolist()[:4] == [False, True, True, False]
    assert flags["in_rollover_window"].tolist()[4:] == [False, True, True, True, False]


@pytest.mark.parametrize(
    ("utc", "inside"),
    [
        ("2024-03-10 21:59", True),  # Sunday 17:59 EDT (the day DST starts): before the reopen
        ("2024-03-10 22:00", True),  # the Sunday reopen follows no rollover, but is covered
        ("2024-03-10 22:15", False),
        ("2024-01-16 21:44", False),  # 16:44 EST in winter
        ("2024-01-16 21:45", True),
        ("2024-01-16 23:14", True),
        ("2024-01-16 23:15", False),
    ],
)
def test_rollover_window_is_a_new_york_clock_window(
    sessions: SessionsConfig, utc: str, inside: bool
) -> None:
    assert at(sessions, utc).iloc[0]["in_rollover_window"] == inside


def test_clock_windows_need_no_anchor_and_must_be_ordered(sessions: SessionsConfig) -> None:
    extra = {"tz": "Europe/London", "start": "07:00", "end": "08:00"}
    data = sessions.model_dump(mode="json")
    changed = SessionsConfig.model_validate(
        {**data, "event_windows": {**data["event_windows"], "london_fix_prep": extra}}
    )
    flags = at(changed, "2024-07-01 06:00", "2024-07-01 05:59")  # 07:00 and 06:59 BST
    assert flags["in_london_fix_prep_window"].tolist() == [True, False]
    with pytest.raises(ValueError, match="start must be before"):
        SessionsConfig.model_validate(
            {**data, "event_windows": {"rollover": {**extra, "start": "08:00"}}}
        )
    with pytest.raises(ValueError, match="unknown anchors"):
        SessionsConfig.model_validate(
            {**data, "event_windows": {"nowhere": {"before_min": 1, "after_min": 1}}}
        )


def test_us_holiday_early_close_and_skipped_release(sessions: SessionsConfig) -> None:
    row = at(sessions, "2024-01-15 13:00").iloc[0]  # Martin Luther King Jr. Day, EST
    assert row["is_us_holiday"]
    assert row["is_early_close"]
    assert row["minutes_to_market_close"] == 390  # 14:30 EST = 19:30 UTC (from 2022)
    assert row["minutes_to_us_data_release"] == 1470  # none today; tomorrow 13:30 UTC
    assert row["minutes_since_us_data_release"] == 4290  # Friday 12 January 13:30 UTC


def test_market_close_rolls_into_a_closed_saturday(sessions: SessionsConfig) -> None:
    row = at(sessions, "2024-03-15 21:00").iloc[0]
    assert row["trading_day"] == date(2024, 3, 16)
    assert not row["is_open"]
    assert np.isnan(row["minutes_to_market_close"])
    assert not row["in_new_york"]
    assert row["minutes_since_rollover"] == 0
    assert row["in_rollover_window"]


def test_values_match_the_session_table(sessions: SessionsConfig) -> None:
    times = pd.date_range("2024-03-10 21:00", "2024-03-16 00:00", freq="15min", tz="UTC")
    columns = calendar_columns(times, sessions)
    table = build_session_table(sessions, date(2024, 3, 1), date(2024, 3, 25)).set_index(
        "trading_day"
    )
    pm = table["lbma_pm_utc"].dropna().sort_values()
    for t, row in zip(times, columns.itertuples(), strict=True):
        day = table.loc[trading_day(t)]
        for name in ("tokyo", "london", "new_york", "london_new_york"):
            opens, closes = day[f"{name}_open_utc"], day[f"{name}_close_utc"]
            expected = bool(pd.notna(opens) and opens <= t < closes)
            assert getattr(row, f"in_{name}") == expected, (t, name)
        upcoming = pm[pm >= t]
        expected_to = (upcoming.iloc[0] - t) / pd.Timedelta(minutes=1)
        assert row.minutes_to_lbma_pm == expected_to, t


def test_truncation_invariance(sessions: SessionsConfig) -> None:
    times = pd.date_range("2024-03-11 00:00", "2024-03-29 00:00", freq="1h", tz="UTC")
    full = calendar_columns(times, sessions)
    for cut in (1, 50, 200, len(times) - 1):
        part = calendar_columns(times[:cut], sessions)
        pd.testing.assert_frame_equal(part, full.iloc[:cut])


def test_empty_and_naive_inputs(sessions: SessionsConfig) -> None:
    assert calendar_columns(pd.DatetimeIndex([], tz="UTC"), sessions).empty
    with pytest.raises(ValueError, match="tz-aware"):
        calendar_columns(pd.DatetimeIndex(["2024-03-12 10:00"]), sessions)
