"""Calendar and session data-quality checks (DQ-004).

They compare the data with the trading calendar (ADR 0002): quotes while the market is closed,
missing quotes while it is open, behaviour on holidays and early closes, and where the daily and
weekly gaps sit. A clock-convention error (ADR 0003) shows up here as boundaries an hour off.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from xq.data.flags import TickFlag
from xq.quality.checks.common import flag_mask, market_hours, tick_anomalies, utc
from xq.quality.registry import Anomaly, Measurement, PartitionData, Scope, register

_MINUTE_NS = 60 * 1_000_000_000
_HOUR_NS = 60 * _MINUTE_NS


@register
class ClosedMarketTicks:
    """Share of the day's ticks outside the calendar's market hours."""

    check_id = "cal.closed_market_ticks"
    scope = Scope.PARTITION
    description = "ticks while the calendar says the market is closed"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        if data.ticks.empty:
            return None
        closed = flag_mask(data.ticks, TickFlag.CLOSED_MARKET)
        return Measurement(
            float(closed.mean()),
            {
                "ticks": len(data.ticks),
                "closed_market_ticks": int(closed.sum()),
                "market_open_day": bool(data.day["is_open"]),
            },
            tick_anomalies(data.ticks, closed, "outside market hours"),
        )


@register
class HolidayBehaviour:
    """Ticks outside market hours on holidays and early-close days."""

    check_id = "cal.holiday_behaviour"
    scope = Scope.PARTITION
    description = "ticks outside market hours on a holiday or early-close trading day"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        holiday = data.day.get("holiday")
        special = bool(data.day["is_early_close"]) or (isinstance(holiday, str) and bool(holiday))
        if not special or data.ticks.empty:
            return None
        closed = flag_mask(data.ticks, TickFlag.CLOSED_MARKET)
        return Measurement(
            float(closed.sum()),
            {
                "holiday": holiday,
                "early_close": bool(data.day["is_early_close"]),
                "market_open_day": bool(data.day["is_open"]),
            },
            tick_anomalies(data.ticks, closed, f"outside hours on {holiday or 'early close'}"),
        )


@register
class MissingOpenMarketData:
    """Share of whole minutes within market hours that have no 1-minute bar."""

    check_id = "cal.missing_open_data"
    scope = Scope.PARTITION
    description = "minutes within market hours (all sessions) without a 1-minute bar"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        hours = market_hours(data)
        if hours is None:
            return None
        opens, closes = _within_coverage(hours, data.coverage)
        expected = np.arange(
            -(-opens // _MINUTE_NS) * _MINUTE_NS,
            closes - _MINUTE_NS + 1,
            _MINUTE_NS,
            dtype=np.int64,
        )
        if len(expected) == 0:
            return None
        present = data.bars_1m["bar_start_utc"].to_numpy(dtype=np.int64)
        missing = expected[~np.isin(expected, present)]
        hour_starts, per_hour = np.unique(missing // _HOUR_NS * _HOUR_NS, return_counts=True)
        anomalies = [
            Anomaly(utc(h), float(n), "missing minutes in this hour")
            for h, n in zip(hour_starts, per_hour, strict=True)
        ]
        return Measurement(
            len(missing) / len(expected),
            {"expected_minutes": len(expected), "missing_minutes": len(missing)},
            anomalies,
        )


@register
class GapLocation:
    """Minutes between the calendar's open/close and the day's first/last tick (the larger)."""

    check_id = "cal.gap_location"
    scope = Scope.PARTITION
    description = "distance of the first and last tick from the calendar's market open and close"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        hours = market_hours(data)
        if hours is None or data.ticks.empty:
            return None
        opens, closes = hours
        ts = data.ticks["ts_utc"].to_numpy(dtype=np.int64)
        first, last = int(ts.min()), int(ts.max())
        coverage_start, coverage_end = data.coverage or (first, last)
        deviations: dict[str, float] = {}
        # Skip a boundary the source's data does not reach: an export edge is not a clock error.
        if coverage_start < opens:
            deviations["open"] = (first - opens) / _MINUTE_NS
        if coverage_end >= closes:
            deviations["close"] = (last - closes) / _MINUTE_NS
        if not deviations:
            return None
        anomalies = [
            Anomaly(
                utc(first if side == "open" else last),
                value,
                f"minutes from the calendar {side} (negative = before)",
            )
            for side, value in deviations.items()
        ]
        metric = max(abs(v) for v in deviations.values())
        return Measurement(metric, {f"{k}_minutes": v for k, v in deviations.items()}, anomalies)


def _within_coverage(hours: tuple[int, int], coverage: tuple[int, int] | None) -> tuple[int, int]:
    if coverage is None:
        return hours
    return max(hours[0], coverage[0]), min(hours[1], coverage[1] + 1)
