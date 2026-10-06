"""ADR 0070: the owner's calendar decisions C1-C8 on the DQ-008 review (calendar version s2)."""

from datetime import date, time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from helpers.ticks import dense_ticks
from xq.core.config import DatedTime, SessionsConfig, load_config, time_on
from xq.core.time import to_ns
from xq.data.calendar import MarketCalendar
from xq.data.clean import MarketWindow, clean_ticks
from xq.data.flags import TickFlag

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"


@pytest.fixture(scope="module")
def cfg() -> SessionsConfig:
    return load_config("research", config_dir=REPO_CONFIG).sessions_config()


@pytest.fixture(scope="module")
def calendar(cfg: SessionsConfig) -> MarketCalendar:
    return MarketCalendar(cfg, 2014, 2025)


def utc(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


def test_the_calendar_is_version_s2(cfg: SessionsConfig) -> None:
    assert cfg.version == "s2"


@pytest.mark.parametrize(
    ("day", "close_utc"),
    [
        # C3: holiday early closes at 13:00 New York through 2021-12-31 ...
        (date(2014, 1, 20), "2014-01-20 18:00"),  # Martin Luther King Jr. Day, EST
        (date(2021, 1, 18), "2021-01-18 18:00"),
        (date(2021, 7, 5), "2021-07-05 17:00"),  # Independence Day (observed), EDT
        (date(2021, 11, 25), "2021-11-25 18:00"),  # Thanksgiving Day
        # ... and at 14:30 New York from 2022-01-01.
        (date(2022, 1, 17), "2022-01-17 19:30"),
        (date(2022, 7, 4), "2022-07-04 18:30"),
        (date(2025, 1, 20), "2025-01-20 19:30"),
        # C8: irregular closes are not modelled: 2019-07-04 (traded to 16:56) follows the rule.
        (date(2019, 7, 4), "2019-07-04 17:00"),
    ],
)
def test_holiday_early_close_time_depends_on_the_date(
    calendar: MarketCalendar, day: date, close_utc: str
) -> None:
    status = calendar.status(day)
    assert status.is_open
    assert status.is_early_close
    assert status.holiday is not None
    assert status.market_close_utc == utc(close_utc)


def test_new_years_eve_is_a_full_trading_day(calendar: MarketCalendar) -> None:
    # C4: seven occurrences in the review, all traded to 16:58-16:59 New York.
    for year in (2014, 2015, 2018, 2019, 2020, 2021, 2024):
        status = calendar.status(date(year, 12, 31))
        assert status.is_open
        assert not status.is_early_close
        assert status.market_close_utc == utc(f"{year}-12-31 22:00")


def test_christmas_eve_closes_at_13_45(calendar: MarketCalendar) -> None:
    # C5: 13:45 New York, whatever the year (a 2022 change applies to holidays only).
    for year in (2015, 2019, 2024):
        status = calendar.status(date(year, 12, 24))
        assert status.is_early_close
        assert status.holiday is None
        assert status.market_close_utc == utc(f"{year}-12-24 18:45")


@pytest.mark.parametrize("day", [date(2014, 11, 28), date(2021, 11, 26), date(2024, 11, 29)])
def test_the_day_after_thanksgiving_closes_at_13_45(calendar: MarketCalendar, day: date) -> None:
    # C6: an early close at 13:45 New York (EST), not a holiday.
    status = calendar.status(day)
    assert status.is_open
    assert status.is_early_close
    assert status.holiday is None
    assert status.market_close_utc == utc(f"{day.isoformat()} 18:45")


@pytest.mark.parametrize(
    ("day", "name"),
    [
        (date(2018, 12, 5), "National Day of Mourning for former President George H. W. Bush"),
        (date(2025, 1, 9), "National Day of Mourning for former President Jimmy Carter"),
    ],
)
def test_national_days_of_mourning_are_full_trading_days(
    calendar: MarketCalendar, day: date, name: str
) -> None:
    # C7: named exceptions; the day is still a US-calendar holiday (so US releases are skipped).
    status = calendar.status(day)
    assert status.holiday == name
    assert status.is_open
    assert not status.is_early_close
    assert status.market_close_utc == utc(f"{day.isoformat()} 22:00")


def test_market_hours_and_full_closes_are_unchanged(calendar: MarketCalendar) -> None:
    # C1: 18:00-17:00 New York, Sunday open 18:00; C2: three full-close holidays.
    regular = calendar.status(date(2024, 3, 12))
    assert (regular.market_open_utc, regular.market_close_utc) == (
        utc("2024-03-11 22:00"),
        utc("2024-03-12 21:00"),
    )
    assert calendar.status(date(2024, 3, 11)).market_open_utc == utc("2024-03-10 22:00")
    for day in (date(2024, 1, 1), date(2024, 3, 29), date(2024, 12, 25), date(2022, 12, 26)):
        assert not calendar.status(day).is_open


def test_ticks_after_13_30_on_a_2025_holiday_are_no_longer_closed_market(
    calendar: MarketCalendar,
) -> None:
    # The review's worst case: 2025-01-20 (MLK Day) traded to 14:29 New York; 4,755 ticks were
    # flagged CLOSED_MARKET under the old 13:30 close. Under s2 only ticks from 14:30 are.
    status = calendar.status(date(2025, 1, 20))
    assert status.market_open_utc is not None
    assert status.market_close_utc is not None
    market = MarketWindow(to_ns(status.market_open_utc), to_ns(status.market_close_utc))
    ticks = dense_ticks("2025-01-20 18:00", "2025-01-20 20:00", seed=3, mean_interval_s=30)
    flagged = clean_ticks(
        ticks, load_config("research", config_dir=REPO_CONFIG).cleaning_config(), market
    )
    closed = (flagged.ticks["flags"].to_numpy() & TickFlag.CLOSED_MARKET) != 0
    after = flagged.ticks["ts_utc"].to_numpy() >= to_ns(utc("2025-01-20 19:30"))
    assert after.any()
    assert (closed == after).all()


def test_time_on_picks_the_latest_entry_in_force() -> None:
    schedule = (DatedTime(time=time(13)), DatedTime(since=date(2022, 1, 1), time=time(14, 30)))
    assert time_on(schedule, date(2021, 12, 31)) == time(13)
    assert time_on(schedule, date(2022, 1, 1)) == time(14, 30)


def with_holidays(cfg: SessionsConfig, **changes: Any) -> dict[str, Any]:
    data = cfg.model_dump(mode="json")
    return {**data, "holidays": {**data["holidays"], **changes}}


def test_a_bare_time_is_one_schedule_for_every_date(cfg: SessionsConfig) -> None:
    changed = SessionsConfig.model_validate(with_holidays(cfg, early_close_time="13:30"))
    assert changed.holidays.early_close_time == (DatedTime(time=time(13, 30)),)


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        ([], "at least one entry"),
        ([{"since": "2020-01-01", "time": "13:00"}], "first entry"),
        (
            [{"time": "13:00"}, {"since": "2022-01-01", "time": "14:30"}, {"time": "15:00"}],
            "strictly increasing",
        ),
        (
            [
                {"time": "13:00"},
                {"since": "2022-01-01", "time": "14:30"},
                {"since": "2021-01-01", "time": "15:00"},
            ],
            "strictly increasing",
        ),
        ([{"time": 780}], 'quoted "HH:MM"'),
    ],
)
def test_malformed_schedules_are_rejected(
    cfg: SessionsConfig, schedule: list[Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        SessionsConfig.model_validate(with_holidays(cfg, early_close_time=schedule))


def test_a_holiday_cannot_be_both_closed_and_a_full_day(cfg: SessionsConfig) -> None:
    with pytest.raises(ValidationError, match="both closed and full"):
        SessionsConfig.model_validate(with_holidays(cfg, full_days=["Good Friday"]))


def test_the_calendar_needs_a_version(cfg: SessionsConfig) -> None:
    data = cfg.model_dump(mode="json")
    data.pop("version")
    with pytest.raises(ValidationError, match="version"):
        SessionsConfig.model_validate(data)
