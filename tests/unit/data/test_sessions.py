"""DATA-002: trading calendar, sessions and event anchors in UTC across DST changes."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from xq.core.config import SessionsConfig, load_config
from xq.data.calendar import MarketCalendar
from xq.data.sessions import build_session_table

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"


@pytest.fixture(scope="module")
def cfg() -> SessionsConfig:
    return load_config("research", config_dir=REPO_CONFIG).sessions_config()


@pytest.fixture(scope="module")
def table(cfg: SessionsConfig) -> pd.DataFrame:
    return build_session_table(cfg, date(2024, 1, 1), date(2024, 12, 31)).set_index("trading_day")


def utc(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


def hours(row: pd.Series, prefix: str) -> tuple[str, str]:
    return (
        row[f"{prefix}_open_utc"].strftime("%m-%d %H:%M"),
        row[f"{prefix}_close_utc"].strftime("%m-%d %H:%M"),
    )


# US DST 2024: 10 Mar - 3 Nov. UK summer time 2024: 31 Mar - 27 Oct.
@pytest.mark.parametrize(
    ("day", "market", "london", "new_york", "overlap"),
    [
        # Both on standard time.
        (
            date(2024, 3, 8),
            ("03-07 23:00", "03-08 22:00"),
            ("03-08 08:00", "03-08 17:00"),
            ("03-08 13:00", "03-08 22:00"),
            ("03-08 13:00", "03-08 17:00"),
        ),
        # New York on EDT, London still on GMT: a five-hour overlap. Monday opens on Sunday.
        (
            date(2024, 3, 11),
            ("03-10 22:00", "03-11 21:00"),
            ("03-11 08:00", "03-11 17:00"),
            ("03-11 12:00", "03-11 21:00"),
            ("03-11 12:00", "03-11 17:00"),
        ),
        # Both on summer time.
        (
            date(2024, 4, 2),
            ("04-01 22:00", "04-02 21:00"),
            ("04-02 07:00", "04-02 16:00"),
            ("04-02 12:00", "04-02 21:00"),
            ("04-02 12:00", "04-02 16:00"),
        ),
        # London back on GMT, New York still on EDT.
        (
            date(2024, 10, 29),
            ("10-28 22:00", "10-29 21:00"),
            ("10-29 08:00", "10-29 17:00"),
            ("10-29 12:00", "10-29 21:00"),
            ("10-29 12:00", "10-29 17:00"),
        ),
        # Both back on standard time; Monday after the US change opens Sunday 18:00 EST.
        (
            date(2024, 11, 4),
            ("11-03 23:00", "11-04 22:00"),
            ("11-04 08:00", "11-04 17:00"),
            ("11-04 13:00", "11-04 22:00"),
            ("11-04 13:00", "11-04 17:00"),
        ),
    ],
)
def test_sessions_across_us_and_uk_dst_changes(
    table: pd.DataFrame,
    day: date,
    market: tuple[str, str],
    london: tuple[str, str],
    new_york: tuple[str, str],
    overlap: tuple[str, str],
) -> None:
    row = table.loc[day]
    assert row["is_open"]
    assert hours(row, "market") == market
    assert hours(row, "london") == london
    assert hours(row, "new_york") == new_york
    assert hours(row, "london_new_york") == overlap
    # Tokyo has no DST: always 00:00-09:00 UTC.
    assert hours(row, "tokyo") == (day.strftime("%m-%d 00:00"), day.strftime("%m-%d 09:00"))


def test_every_open_day_lies_inside_its_trading_day(table: pd.DataFrame) -> None:
    open_days = table[table["is_open"]]
    assert (open_days["market_open_utc"] >= open_days["day_start_utc"]).all()
    assert (open_days["market_close_utc"] <= open_days["day_end_utc"]).all()
    for session in ("tokyo", "london", "new_york", "london_new_york"):
        present = open_days[open_days[f"{session}_open_utc"].notna()]
        assert (present[f"{session}_open_utc"] >= present["market_open_utc"]).all()
        assert (present[f"{session}_close_utc"] <= present["market_close_utc"]).all()


def test_trading_day_bounds_follow_new_york_dst(table: pd.DataFrame) -> None:
    assert table.loc[date(2024, 3, 8), "day_end_utc"] == utc("2024-03-08 22:00")
    assert table.loc[date(2024, 3, 11), "day_start_utc"] == utc("2024-03-10 21:00")
    assert table.loc[date(2024, 3, 11), "day_end_utc"] == utc("2024-03-11 21:00")


def test_weekends_are_closed(table: pd.DataFrame) -> None:
    weekend = table[[d.weekday() >= 5 for d in table.index]]
    assert len(weekend) == 104
    assert not weekend["is_open"].any()
    assert weekend["market_open_utc"].isna().all()
    assert weekend["lbma_am_utc"].isna().all()
    assert weekend["rollover_utc"].isna().all()


@pytest.mark.parametrize(
    ("day", "holiday"),
    [
        (date(2024, 1, 1), "New Year's Day"),
        (date(2024, 3, 29), "Good Friday"),
        (date(2024, 12, 25), "Christmas Day"),
    ],
)
def test_full_close_holidays(table: pd.DataFrame, day: date, holiday: str) -> None:
    row = table.loc[day]
    assert not row["is_open"]
    assert row["holiday"] == holiday
    assert pd.isna(row["market_open_utc"])
    assert pd.isna(row["new_york_open_utc"])


def test_day_after_a_closed_holiday_opens_at_the_normal_time(table: pd.DataFrame) -> None:
    assert table.loc[date(2024, 12, 26), "market_open_utc"] == utc("2024-12-25 23:00")


def test_us_holiday_early_close(table: pd.DataFrame) -> None:
    row = table.loc[date(2024, 11, 28)]  # Thanksgiving
    assert row["is_open"]
    assert row["is_early_close"]
    assert row["holiday"] == "Thanksgiving Day"
    assert row["market_close_utc"] == utc("2024-11-28 19:30")  # 14:30 EST (from 2022)
    assert row["new_york_close_utc"] == utc("2024-11-28 19:30")  # clipped to the early close
    assert pd.isna(row["us_data_release_utc"])  # no US releases on US holidays
    assert pd.isna(row["comex_open_utc"])
    assert row["lbma_am_utc"] == utc("2024-11-28 10:30")  # London works as usual
    assert row["rollover_utc"] == utc("2024-11-28 22:00")  # financing still rolls at 17:00


def test_christmas_eve_early_close_and_single_lbma_auction(table: pd.DataFrame) -> None:
    row = table.loc[date(2024, 12, 24)]
    assert row["is_early_close"]
    assert row["holiday"] is None
    assert row["market_close_utc"] == utc("2024-12-24 18:45")  # 13:45 EST (C5)
    assert row["lbma_am_utc"] == utc("2024-12-24 10:30")
    assert pd.isna(row["lbma_pm_utc"])


def test_uk_bank_holiday_skips_lbma_only(table: pd.DataFrame) -> None:
    row = table.loc[date(2024, 5, 6)]  # Early May bank holiday
    assert row["is_open"]
    assert row["uk_holiday"] == "May Day"
    assert pd.isna(row["lbma_am_utc"])
    assert pd.isna(row["lbma_pm_utc"])
    assert row["london_open_utc"] == utc("2024-05-06 07:00")
    assert row["us_data_release_utc"] == utc("2024-05-06 12:30")


@pytest.mark.parametrize(
    ("day", "lbma_am", "lbma_pm", "comex", "us_data", "rollover"),
    [
        (date(2024, 3, 8), "10:30", "15:00", "13:20", "13:30", "22:00"),
        (date(2024, 3, 11), "10:30", "15:00", "12:20", "12:30", "21:00"),
        (date(2024, 4, 2), "09:30", "14:00", "12:20", "12:30", "21:00"),
        (date(2024, 11, 4), "10:30", "15:00", "13:20", "13:30", "22:00"),
    ],
)
def test_event_anchors_across_dst(
    table: pd.DataFrame,
    day: date,
    lbma_am: str,
    lbma_pm: str,
    comex: str,
    us_data: str,
    rollover: str,
) -> None:
    row = table.loc[day]
    for column, expected in [
        ("lbma_am_utc", lbma_am),
        ("lbma_pm_utc", lbma_pm),
        ("comex_open_utc", comex),
        ("us_data_release_utc", us_data),
        ("rollover_utc", rollover),
    ]:
        assert row[column].strftime("%H:%M") == expected, column


def test_observed_holiday_uses_the_observed_date(cfg: SessionsConfig) -> None:
    # Christmas 2021 fell on a Saturday; NYSE observed it on Friday 24 December.
    status = MarketCalendar.for_range(cfg, date(2021, 12, 1), date(2021, 12, 31)).status(
        date(2021, 12, 24)
    )
    assert not status.is_open
    assert status.holiday == "Christmas Day (observed)"


def test_calendar_refuses_days_outside_its_years(cfg: SessionsConfig) -> None:
    calendar = MarketCalendar.for_range(cfg, date(2024, 1, 1), date(2024, 12, 31))
    with pytest.raises(ValueError, match="outside"):
        calendar.status(date(2026, 1, 5))


def test_timestamp_columns_are_utc_nanoseconds(table: pd.DataFrame) -> None:
    for column in table.columns:
        if column.endswith("_utc"):
            assert str(table[column].dtype) == "datetime64[ns, UTC]", column


def test_unquoted_yaml_times_are_rejected(cfg: SessionsConfig) -> None:
    data = cfg.model_dump()
    data["market"]["close"] = 1020  # what YAML 1.1 makes of an unquoted 17:00
    with pytest.raises(ValidationError, match='quoted "HH:MM"'):
        SessionsConfig.model_validate(data)


def test_unknown_time_zone_is_rejected(cfg: SessionsConfig) -> None:
    data = cfg.model_dump()
    data["sessions"]["london"]["tz"] = "Europe/Londn"
    with pytest.raises(ValidationError, match="unknown IANA time zone"):
        SessionsConfig.model_validate(data)


def test_overlap_must_reference_known_sessions(cfg: SessionsConfig) -> None:
    data = cfg.model_dump()
    data["overlaps"] = {"bad": ["london", "sydney"]}
    with pytest.raises(ValidationError, match="overlap 'bad'"):
        SessionsConfig.model_validate(data)
