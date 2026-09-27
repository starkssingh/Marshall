"""Trading calendar: open days, holidays, early closes and market hours in UTC (DATA-002).

Holiday dates come from the `holidays` package (US financial calendar, observed dates included;
English bank holidays for LBMA). Which holidays close the market and when early closes happen is
configuration (`config/sessions.yaml`), because it differs between venues.

`MarketClock` measures trading time — only the market-open intervals count — for horizons that
skip the daily break, weekends and holidays (ADR 0026).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta

import holidays as holiday_calendars
import numpy as np
import numpy.typing as npt
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


def regular_trading_day(cfg: SessionsConfig) -> pd.Timedelta:
    """Market time of a full trading day: from the open to the close (23 h for 18:00-17:00).

    Early closes do not change it: it is the length of the configured regular session, the unit
    of trading-day horizons (ADR 0032).
    """
    market = cfg.market
    day = 24 * 3600
    seconds = [t.hour * 3600 + t.minute * 60 + t.second for t in (market.open, market.close)]
    span = (seconds[1] - seconds[0]) % day
    return pd.Timedelta(seconds=span or day)


#: Integer sentinel for "no instant" in int64 nanosecond arrays (the value of ``NaT``).
NAT_NS = np.iinfo(np.int64).min


class MarketClock:
    """Trading time over the market-open intervals of a range of trading days (ADR 0026).

    Each open trading day contributes ``[market_open_utc, market_close_utc)``; the daily break,
    weekends and closed holidays contribute nothing, and early closes shorten their day. Instants
    are int64 UTC nanoseconds. The clock covers ``[covered_from, covered_to)``: the trading days
    it was built for.
    """

    def __init__(
        self,
        opens: npt.NDArray[np.int64],
        closes: npt.NDArray[np.int64],
        covered_from: int,
        covered_to: int,
    ) -> None:
        if len(opens) != len(closes) or np.any(closes <= opens):
            raise ValueError("market intervals need an open before each close")
        if np.any(opens[1:] < closes[:-1]):
            raise ValueError("market intervals must be sorted and must not overlap")
        self.opens = opens
        self.closes = closes
        self.covered_from = covered_from
        self.covered_to = covered_to
        lengths = closes - opens
        self._elapsed_at_open = np.concatenate([[0], np.cumsum(lengths)[:-1]]).astype(np.int64)
        self._elapsed_at_close = self._elapsed_at_open + lengths

    @classmethod
    def for_range(cls, cfg: SessionsConfig, start: date, end: date) -> MarketClock:
        """The clock of trading days `start` to `end` inclusive."""
        calendar = MarketCalendar.for_range(cfg, start, end)
        opens, closes = [], []
        for offset in range((end - start).days + 1):
            status = calendar.status(start + timedelta(days=offset))
            if status.market_open_utc is not None and status.market_close_utc is not None:
                opens.append(status.market_open_utc.value)
                closes.append(status.market_close_utc.value)
        return cls(
            np.array(opens, dtype=np.int64),
            np.array(closes, dtype=np.int64),
            trading_day_bounds(start)[0].value,
            trading_day_bounds(end)[1].value,
        )

    def elapsed(self, t: npt.NDArray[np.int64]) -> npt.NDArray[np.int64]:
        """Market time from the start of the covered range to each instant (nanoseconds)."""
        self._check_covered(t)
        k = np.searchsorted(self.opens, t, side="right") - 1
        inside = k >= 0
        out = np.zeros(len(t), dtype=np.int64)
        kk = k[inside]
        out[inside] = (
            self._elapsed_at_open[kk] + np.minimum(t[inside], self.closes[kk]) - (self.opens[kk])
        )
        return out

    def advance(self, t: npt.NDArray[np.int64], duration: int) -> npt.NDArray[np.int64]:
        """The first market-open instant at which `duration` of market time has passed since t.

        A t while the market is closed starts counting at the next open, so ``advance(t, 0)`` is
        t itself when the market is open and the next open otherwise; a result landing exactly on
        a close moves to the next open. `NAT_NS` where the answer lies beyond the covered range.
        """
        if duration < 0:
            raise ValueError("duration must be non-negative")
        target = self.elapsed(t) + duration
        k = np.searchsorted(self._elapsed_at_close, target, side="right")
        known = k < len(self.opens)
        out = np.full(len(t), NAT_NS, dtype=np.int64)
        kk = k[known]
        out[known] = self.opens[kk] + target[known] - self._elapsed_at_open[kk]
        return out

    def crosses_close(
        self, start: npt.NDArray[np.int64], end: npt.NDArray[np.int64]
    ) -> npt.NDArray[np.bool_]:
        """Whether a market close c lies in ``start <= c < end`` (a position held over it)."""
        before_end = np.searchsorted(self.closes, end, side="left")
        before_start = np.searchsorted(self.closes, start, side="left")
        crosses: npt.NDArray[np.bool_] = before_end > before_start
        return crosses

    def _check_covered(self, t: npt.NDArray[np.int64]) -> None:
        if len(t) and (t.min() < self.covered_from or t.max() >= self.covered_to):
            raise ValueError(
                f"instants from {pd.Timestamp(int(t.min()), tz='UTC')} to "
                f"{pd.Timestamp(int(t.max()), tz='UTC')} are outside the market clock's range"
            )
