"""Trading calendar: open days, holidays, early closes and market hours in UTC (DATA-002).

Holiday dates come from the `holidays` package (US financial calendar, observed dates included;
English bank holidays for LBMA). Which holidays close the market and when early closes happen is
configuration (`config/sessions.yaml`), because it differs between venues.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta

import holidays as holiday_calendars
import pandas as pd

from xq.core.config import WEEKDAYS, SessionsConfig
from xq.core.errors import ConfigError
from xq.core.time import local_time_to_utc, trading_day_bounds


@dataclass(frozen=True)
class DayStatus:
    """Market status of one trading day. Timestamps are UTC; market hours are None when closed."""

    trading_day: date
    is_open: bool
    holiday: str | None
    uk_holiday: str | None
    is_early_close: bool
    day_start_utc: pd.Timestamp
    day_end_utc: pd.Timestamp
    market_open_utc: pd.Timestamp | None
    market_close_utc: pd.Timestamp | None

    def is_market_open_at(self, ts: pd.Timestamp) -> bool:
        """True if `ts` lies within ``[market_open_utc, market_close_utc)``."""
        if self.market_open_utc is None or self.market_close_utc is None:
            return False
        return bool(self.market_open_utc <= ts < self.market_close_utc)


class MarketCalendar:
    """Answers "is the market open on trading day D, and when?" for a range of years."""

    def __init__(self, cfg: SessionsConfig, first_year: int, last_year: int) -> None:
        # One year of padding either side covers observed dates that cross a year boundary.
        years = list(range(first_year - 1, last_year + 2))
        rules = cfg.holidays
        self._cfg = cfg
        self._us = holiday_calendars.financial_holidays(rules.us_calendar, years=years)
        self._uk = holiday_calendars.country_holidays(
            rules.uk_country, subdiv=rules.uk_subdiv, years=years
        )
        self._years = range(first_year, last_year + 1)
        self._closed_prefixes = tuple(name.casefold() for name in rules.closed)

    @classmethod
    def for_range(cls, cfg: SessionsConfig, start: date, end: date) -> MarketCalendar:
        """Build a calendar covering trading days `start` to `end` inclusive."""
        return cls(cfg, start.year, end.year)

    def us_holiday(self, day: date) -> str | None:
        """Name of the US financial-calendar holiday on `day`, if any."""
        self._check_covered(day)
        name: str | None = self._us.get(day)
        return name

    def uk_holiday(self, day: date) -> str | None:
        """Name of the English bank holiday on `day`, if any."""
        self._check_covered(day)
        name: str | None = self._uk.get(day)
        return name

    def is_trading_weekday(self, day: date) -> bool:
        """True if trading days on this weekday are normally open."""
        return WEEKDAYS[day.weekday()] in self._cfg.market.trading_weekdays

    def status(self, day: date) -> DayStatus:
        """Return the market status of trading day `day`."""
        market = self._cfg.market
        rules = self._cfg.holidays
        holiday = self.us_holiday(day)
        uk_holiday = self.uk_holiday(day)
        day_start, day_end = trading_day_bounds(day)

        closed_holiday = holiday is not None and holiday.casefold().startswith(
            self._closed_prefixes
        )
        if not self.is_trading_weekday(day) or closed_holiday:
            return DayStatus(day, False, holiday, uk_holiday, False, day_start, day_end, None, None)

        early = holiday is not None or day.strftime("%m-%d") in rules.early_close_dates
        after_break = self.is_trading_weekday(day - timedelta(days=1))
        open_time = market.open if after_break else market.week_open
        close_time = rules.early_close_time if early else market.close
        market_open = self._within_day(day, open_time, closing=False)
        market_close = self._within_day(day, close_time, closing=True)
        if market_open >= market_close:
            raise ConfigError(f"market hours on {day} are empty: {market_open} >= {market_close}")
        return DayStatus(
            day, True, holiday, uk_holiday, early, day_start, day_end, market_open, market_close
        )

    def _within_day(self, day: date, at: time, *, closing: bool) -> pd.Timestamp:
        """Place local time `at` on the calendar date that puts it inside trading day `day`."""
        start, end = trading_day_bounds(day)
        for calendar_day in (day - timedelta(days=1), day):
            instant = local_time_to_utc(calendar_day, at, self._cfg.market.tz)
            inside = start < instant <= end if closing else start <= instant < end
            if inside:
                return instant
        raise ConfigError(
            f"market time {at} ({self._cfg.market.tz}) does not fall inside trading day {day}"
        )

    def _check_covered(self, day: date) -> None:
        if day.year not in self._years:
            raise ValueError(f"{day} is outside the calendar's years {self._years}")
