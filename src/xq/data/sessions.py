"""Per-trading-day session table in UTC (DATA-002).

Sessions and event anchors are defined in local time (``config/sessions.yaml``) and converted to
UTC for each date, so DST changes in New York, London and Tokyo are handled by construction.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from xq.core.config import EventAnchor, SessionsConfig, SessionWindow
from xq.core.time import local_time_to_utc
from xq.data.calendar import DayStatus, MarketCalendar

TIMESTAMP_DTYPE = "datetime64[ns, UTC]"
Window = tuple[pd.Timestamp | None, pd.Timestamp | None]


def build_session_table(cfg: SessionsConfig, start: date, end: date) -> pd.DataFrame:
    """Return one row per trading day from `start` to `end` inclusive (weekends included).

    Columns:
        trading_day, is_open, holiday, uk_holiday, is_early_close: day status.
        day_start_utc, day_end_utc: the trading day's ``[17:00, 17:00)`` New York interval.
        market_open_utc, market_close_utc: when the venue quotes (NaT when closed).
        ``<session>_open_utc`` / ``<session>_close_utc``: sessions clipped to market hours
        (NaT if the session does not intersect them), including configured overlaps.
        ``<anchor>_utc``: event anchors (NaT when skipped or outside market hours).
    """
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    calendar = MarketCalendar.for_range(cfg, start, end)
    rows = [_row(cfg, calendar.status(day)) for day in _days(start, end)]
    table = pd.DataFrame(rows)
    for column in table.columns:
        if column.endswith("_utc"):
            table[column] = pd.Series(table[column].tolist(), dtype=TIMESTAMP_DTYPE)
    return table


def _row(cfg: SessionsConfig, status: DayStatus) -> dict[str, Any]:
    row: dict[str, Any] = {
        "trading_day": status.trading_day,
        "is_open": status.is_open,
        "holiday": status.holiday,
        "uk_holiday": status.uk_holiday,
        "is_early_close": status.is_early_close,
        "day_start_utc": status.day_start_utc,
        "day_end_utc": status.day_end_utc,
        "market_open_utc": status.market_open_utc,
        "market_close_utc": status.market_close_utc,
    }
    windows: dict[str, Window] = {}
    for name, window in cfg.sessions.items():
        windows[name] = _session(status, window)
    for name, members in cfg.overlaps.items():
        windows[name] = _intersect([windows[member] for member in members])
    for name, (opens, closes) in windows.items():
        row[f"{name}_open_utc"] = opens
        row[f"{name}_close_utc"] = closes
    for name, anchor in cfg.event_anchors.items():
        row[f"{name}_utc"] = _anchor(status, anchor)
    return row


def _session(status: DayStatus, window: SessionWindow) -> Window:
    if status.market_open_utc is None or status.market_close_utc is None:
        return None, None
    day = status.trading_day
    opens = local_time_to_utc(day, window.open, window.tz)
    closes = local_time_to_utc(day, window.close, window.tz)
    return _intersect([(opens, closes), (status.market_open_utc, status.market_close_utc)])


def _intersect(windows: list[Window]) -> Window:
    starts = [start for start, _ in windows]
    ends = [end for _, end in windows]
    known_starts = [s for s in starts if s is not None]
    known_ends = [e for e in ends if e is not None]
    if len(known_starts) < len(starts) or len(known_ends) < len(ends):
        return None, None
    start, end = max(known_starts), min(known_ends)
    return (start, end) if start < end else (None, None)


def _anchor(status: DayStatus, anchor: EventAnchor) -> pd.Timestamp | None:
    day = status.trading_day
    if not status.is_open:
        return None
    if "us_holiday" in anchor.skip_on and status.holiday is not None:
        return None
    if "uk_holiday" in anchor.skip_on and status.uk_holiday is not None:
        return None
    if day.strftime("%m-%d") in anchor.skip_dates:
        return None
    instant = local_time_to_utc(day, anchor.time, anchor.tz)
    if anchor.require_open and not status.is_market_open_at(instant):
        return None
    return instant


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
