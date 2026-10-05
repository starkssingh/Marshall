"""Time and event-proximity features (FEAT-006).

Functions of the decision time (the bar's availability) and the calendar configuration only
(``config/sessions.yaml``), so they are known in advance; they read the same per-day session table
as DS-007's calendar columns (`calendar_columns`), so DST is handled by construction:

- ``time_of_day``: ``sin`` and ``cos`` of the local clock time in ``tz`` on a 24-hour circle
  (cyclical hour and minute: 23:59 sits next to 00:00);
- ``day_of_week``: one-hot of the trading day's weekday (17:00 New York roll), ``mon`` … ``fri``;
- ``session``: one-hot flags of every configured session and overlap (``tokyo``, ``london``,
  ``new_york``, ``london_new_york``): 1 while ``open <= t < close`` on the trading day;
- ``event_minutes``: minutes to the next and since the last occurrence of an event anchor
  (``lbma_am``, ``lbma_pm``, ``us_data_release``, ``rollover``, ...), each capped at
  ``cap_minutes`` (also when the next or last occurrence lies beyond the calendar's seven-day
  lookup).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field, field_validator

from xq.core.errors import ConfigError
from xq.core.time import trading_days
from xq.datasets.calendar_columns import calendar_columns
from xq.features.base import (
    BarContext,
    Feature,
    FeatureParams,
    availability_index,
    frame,
    register_feature,
)

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri")
_DAY_MINUTES = 24 * 60


class LocalTime(FeatureParams):
    tz: str

    @field_validator("tz")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            pd.Timestamp("2024-01-01", tz=value)
        except Exception as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value


class NoParams(FeatureParams):
    pass


class Event(FeatureParams):
    anchor: str
    cap_minutes: int = Field(ge=1)


@register_feature(
    name="time_of_day", version=1, family="time", params=LocalTime, lookback=lambda p: 1,
    warmup=lambda p: 1,
)  # fmt: skip
def time_of_day(bars: pd.DataFrame, params: LocalTime, context: BarContext) -> pd.DataFrame:
    """The local clock time on a circle."""
    local = availability_index(bars).tz_convert(params.tz)
    minutes = (local.hour * 60 + local.minute + local.second / 60).to_numpy(np.float64)
    angle = 2 * np.pi * minutes / _DAY_MINUTES
    return frame(bars, sin=np.sin(angle), cos=np.cos(angle))


@register_feature(
    name="day_of_week", version=1, family="time", params=NoParams, lookback=lambda p: 1,
    warmup=lambda p: 1,
)  # fmt: skip
def day_of_week(bars: pd.DataFrame, params: NoParams, context: BarContext) -> pd.DataFrame:
    """One-hot of the trading day's weekday."""
    weekday = np.array([d.item().weekday() for d in trading_days(availability_index(bars))])
    return frame(
        bars, **{name: (weekday == k).astype(float) for k, name in enumerate(WEEKDAY_NAMES)}
    )


@register_feature(
    name="session", version=1, family="time", params=NoParams, lookback=lambda p: 1,
    warmup=lambda p: 1,
)  # fmt: skip
def session(bars: pd.DataFrame, params: NoParams, context: BarContext) -> pd.DataFrame:
    """One-hot flags of the configured sessions and overlaps."""
    columns = calendar_columns(availability_index(bars), context.sessions)
    names = [*context.sessions.sessions, *context.sessions.overlaps]
    return frame(bars, **{n: columns[f"in_{n}"].to_numpy(dtype=float) for n in names})


@register_feature(
    name="event_minutes", version=1, family="time", params=Event, lookback=lambda p: 1,
    warmup=lambda p: 1,
)  # fmt: skip
def event_minutes(bars: pd.DataFrame, params: Event, context: BarContext) -> pd.DataFrame:
    """Minutes to the next and since the last occurrence of an event anchor, capped.

    Raises:
        ConfigError: if the calendar has no such anchor.
    """
    if params.anchor not in context.sessions.event_anchors:
        known = ", ".join(sorted(context.sessions.event_anchors))
        raise ConfigError(f"no event anchor {params.anchor!r} in the calendar; known: {known}")
    columns = calendar_columns(availability_index(bars), context.sessions)
    cap = float(params.cap_minutes)
    to_next = columns[f"minutes_to_{params.anchor}"].fillna(cap).clip(upper=cap)
    since = columns[f"minutes_since_{params.anchor}"].fillna(cap).clip(upper=cap)
    return frame(bars, to=to_next.to_numpy(), since=since.to_numpy())


TIME: tuple[Feature, ...] = (time_of_day, day_of_week, session, event_minutes)
