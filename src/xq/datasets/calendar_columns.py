"""Calendar and session columns known in advance (DS-007).

Every column is a function of the decision time and the calendar configuration only (the
trading calendar, sessions and event anchors in ``config/sessions.yaml``), so it is known before
the decision is taken. Values come from the per-day session table (DATA-002), which converts local
times to UTC per date, so DST is handled by construction.

Columns, for a decision time t in trading day D (``trading_day(t)``, 17:00 New York roll):

- ``trading_day`` and ``day_of_week`` (0 = Monday) of D; ``is_open``, ``is_early_close``,
  ``is_us_holiday``, ``is_uk_holiday``;
- ``minutes_to_market_close``: to D's market close (missing when closed or after it);
- per session and overlap: ``in_<name>`` (open <= t < close on D) and
  ``<name>_minutes_since_open`` (missing outside the session);
- per event anchor: ``minutes_to_<anchor>`` (next occurrence at or after t) and
  ``minutes_since_<anchor>`` (last occurrence at or before t), each looked up at most seven days
  away so a value never depends on how far the data extends;
- per configured event window: ``in_<name>_window``, for ``[anchor - before, anchor + after)``
  around the event anchor of that name, or for local clock times ``[start, end)`` on every day
  (a clock window, ADR 0026; compared on the wall clock, so DST is handled by construction).
"""

from __future__ import annotations

from datetime import time, timedelta

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import ClockWindow, EventWindow, SessionsConfig
from xq.core.time import trading_days
from xq.data.sessions import build_session_table

LOOKUP_HORIZON = pd.Timedelta(days=7)
_MINUTE_NS = 60 * 1_000_000_000
_NAT = np.iinfo(np.int64).min


def calendar_columns(decision_times: pd.DatetimeIndex, cfg: SessionsConfig) -> pd.DataFrame:
    """Calendar columns for each decision time (see the module docstring)."""
    index = pd.DatetimeIndex(decision_times)
    if index.tz is None:
        raise ValueError("decision times must be tz-aware")
    t = index.tz_convert("UTC").as_unit("ns").to_numpy(dtype="datetime64[ns]").view(np.int64)
    out = pd.DataFrame(index=decision_times)
    if len(index) == 0:
        return out
    days = trading_days(index)
    horizon_days = LOOKUP_HORIZON.days + 1
    first = days.min().item() - timedelta(days=horizon_days)
    last = days.max().item() + timedelta(days=horizon_days)
    table = build_session_table(cfg, first, last).set_index("trading_day")
    rows = table.reindex([d.item() for d in days])

    out["trading_day"] = [d.item() for d in days]
    out["day_of_week"] = np.array([d.item().weekday() for d in days], dtype=np.int64)
    out["is_open"] = rows["is_open"].to_numpy(dtype=bool)
    out["is_early_close"] = rows["is_early_close"].to_numpy(dtype=bool)
    out["is_us_holiday"] = rows["holiday"].notna().to_numpy()
    out["is_uk_holiday"] = rows["uk_holiday"].notna().to_numpy()
    close = _ns(rows["market_close_utc"])
    before_close = (close != _NAT) & (t < close)
    out["minutes_to_market_close"] = np.where(before_close, (close - t) / _MINUTE_NS, np.nan)

    for name in [*cfg.sessions, *cfg.overlaps]:
        opens, closes = _ns(rows[f"{name}_open_utc"]), _ns(rows[f"{name}_close_utc"])
        inside = (opens != _NAT) & (opens <= t) & (t < closes)
        out[f"in_{name}"] = inside
        out[f"{name}_minutes_since_open"] = np.where(inside, (t - opens) / _MINUTE_NS, np.nan)

    for name in cfg.event_anchors:
        instants = np.sort(_ns(table[f"{name}_utc"]))
        instants = instants[instants != _NAT]
        to_next, since_last = _around(t, instants)
        out[f"minutes_to_{name}"] = to_next
        out[f"minutes_since_{name}"] = since_last
        window = cfg.event_windows.get(name)
        if isinstance(window, EventWindow):
            out[f"in_{name}_window"] = (to_next <= window.before_min) | (
                since_last < window.after_min
            )
        elif isinstance(window, ClockWindow):
            out[f"in_{name}_window"] = _in_clock_window(index, window)
    for name, window in cfg.event_windows.items():
        if name not in cfg.event_anchors and isinstance(window, ClockWindow):
            out[f"in_{name}_window"] = _in_clock_window(index, window)
    return out


def _in_clock_window(index: pd.DatetimeIndex, window: ClockWindow) -> npt.NDArray[np.bool_]:
    """Whether the local wall-clock time of each instant lies in ``[start, end)``."""
    local = index.tz_convert(window.tz)
    parts = [
        getattr(local, unit).to_numpy(np.int64)
        for unit in ("hour", "minute", "second", "microsecond", "nanosecond")
    ]
    hour, minute, second, micro, nano = parts
    clock = ((hour * 60 + minute) * 60 + second) * 1_000_000_000 + micro * 1000 + nano
    inside: npt.NDArray[np.bool_] = (_clock_ns(window.start) <= clock) & (
        clock < _clock_ns(window.end)
    )
    return inside


def _clock_ns(value: time) -> int:
    """Nanoseconds since local midnight on the wall clock."""
    seconds = (value.hour * 60 + value.minute) * 60 + value.second
    return seconds * 1_000_000_000 + value.microsecond * 1000


def _around(
    t: npt.NDArray[np.int64], instants: npt.NDArray[np.int64]
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Minutes to the next instant at or after t and since the last at or before t (capped)."""
    limit = LOOKUP_HORIZON.value
    to_next = np.full(len(t), np.nan)
    since_last = np.full(len(t), np.nan)
    if len(instants) == 0:
        return to_next, since_last
    nxt = np.searchsorted(instants, t, side="left")
    has_next = nxt < len(instants)
    gap = instants[np.minimum(nxt, len(instants) - 1)] - t
    ok = has_next & (gap <= limit)
    to_next[ok] = gap[ok] / _MINUTE_NS
    prev = np.searchsorted(instants, t, side="right") - 1
    has_prev = prev >= 0
    gap = t - instants[np.maximum(prev, 0)]
    ok = has_prev & (gap <= limit)
    since_last[ok] = gap[ok] / _MINUTE_NS
    return to_next, since_last


def _ns(column: pd.Series) -> npt.NDArray[np.int64]:
    """UTC timestamps as int64 nanoseconds; missing values become the NaT sentinel."""
    values: npt.NDArray[np.int64] = (
        pd.to_datetime(column, utc=True)
        .dt.as_unit("ns")
        .to_numpy(dtype="datetime64[ns]")
        .view(np.int64)
    )
    return values
