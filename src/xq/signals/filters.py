"""Signal filters (SIGNAL-003): which candidates may become trade intents.

Each filter looks at one candidate's decision context (`FilterContext`) and returns why it blocks
it, or None. The signal engine runs every filter a strategy's YAML configures on every candidate
and records each outcome in the candidate's `SignalRecord`, so a rejection always names its
filter.

- **Regime** (`RegimeFilter`): the strategy's allowed regimes, checked against the filtered (never
  smoothed) `RegimeState` at the decision. **No regime model exists yet** (REG-007, Sprint 8), so
  the only implementation is `PassThroughRegimeFilter`, a clearly marked PLACEHOLDER that lets
  every candidate through and says so in every record it touches (ADR 0051).
- **Session** (`SessionFilter`): the decision time lies in one of the allowed sessions or overlaps
  (``config/sessions.yaml``, local times converted to UTC per date).
- **Blackout** (`BlackoutFilter`): the decision time lies in none of the listed event windows
  (e.g. the rollover window and the US data-release window).
- **Volatility band** (`VolatilityBandFilter`): the daily sigma-hat known at the decision lies in
  ``[low, high]``; without a sigma-hat the candidate is blocked.
- **Spread** (`SpreadFilter`): the current spread is **below** ``k`` times its hour-of-week median
  (`HourOfWeekSpreads`, New York hour of week, from spreads observed *before* the decision). While
  that hour has fewer than ``min_obs`` observations the median of all observed spreads stands in;
  with fewer than ``min_obs`` in all, the candidate is blocked.

Sessions and windows are looked up in a per-minute table built once per trading day
(`CalendarLookup`), exact because every configured boundary falls on a whole minute (checked).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time
from typing import Protocol

import pandas as pd

from xq.core.config import SessionsConfig
from xq.core.time import trading_day, trading_day_bounds
from xq.datasets.calendar_columns import calendar_columns
from xq.signals.schema import RegimeState

_MINUTE_NS = 60_000_000_000
_NEW_YORK = "America/New_York"
#: The record entry of a candidate the placeholder regime filter let through.
REGIME_PLACEHOLDER = (
    "PLACEHOLDER pass-through regime filter: no regime model until REG-007 (Sprint 8)"
)


@dataclass(frozen=True)
class FilterContext:
    """What the filters see about one candidate (UTC nanoseconds, prices, fractions of price)."""

    ts: int
    spread: float | None
    sigma_daily: float | None
    regime: RegimeState | None = None


class SignalFilter(Protocol):
    """One filter (module docstring)."""

    @property
    def name(self) -> str:
        """The filter's name in signal records."""
        ...

    @property
    def pass_label(self) -> str:
        """What a signal record says when the filter lets a candidate through."""
        ...

    def check(self, ctx: FilterContext) -> str | None:
        """Why the candidate is blocked, or None."""
        ...


class RegimeFilter(SignalFilter, Protocol):
    """Allowed regimes per strategy, judged on the filtered `RegimeState` at the decision."""

    @property
    def allowed(self) -> tuple[str, ...]:
        """The regime labels the strategy may trade in (empty: any)."""
        ...


class PassThroughRegimeFilter:
    """PLACEHOLDER regime filter: lets every candidate through (module docstring).

    It keeps the strategy's allowed regimes so the YAML is complete, but checks nothing: no
    regime model exists until REG-007 (Sprint 8). Every record says so (`REGIME_PLACEHOLDER`).
    """

    name = "regime"
    pass_label = REGIME_PLACEHOLDER

    def __init__(self, allowed: Sequence[str] = ()) -> None:
        self.allowed = tuple(allowed)

    def check(self, ctx: FilterContext) -> str | None:
        """Nothing is blocked."""
        return None


class CalendarLookup:
    """Session and event-window membership at an instant, from per-minute daily tables."""

    def __init__(self, sessions: SessionsConfig) -> None:
        if not all(t.second == 0 and t.microsecond == 0 for t in _times(sessions.model_dump())):
            raise ValueError("calendar lookups need every configured time on a whole minute")
        self.sessions = sessions
        self._days: dict[date, tuple[int, pd.DataFrame]] = {}

    def row(self, ts: int) -> pd.Series:
        """The calendar columns (`calendar_columns`) of the minute containing `ts`."""
        day = trading_day(pd.Timestamp(ts, tz="UTC"))
        cached = self._days.get(day)
        if cached is None:
            start, end = trading_day_bounds(day)
            grid = pd.date_range(start, end, freq="1min", inclusive="left")
            cached = (start.value, calendar_columns(grid, self.sessions).reset_index(drop=True))
            self._days[day] = cached
        start_ns, table = cached
        return table.iloc[(ts - start_ns) // _MINUTE_NS]

    def in_session(self, ts: int, name: str) -> bool:
        """Whether session or overlap `name` is open at `ts`."""
        return bool(self.row(ts)[f"in_{name}"])

    def in_window(self, ts: int, name: str) -> bool:
        """Whether event window `name` is in force at `ts`."""
        return bool(self.row(ts)[f"in_{name}_window"])


class SessionFilter:
    """The decision time lies in one of `allowed` (sessions or overlaps)."""

    name = "session"
    pass_label = "pass"

    def __init__(self, calendar: CalendarLookup, allowed: Sequence[str]) -> None:
        known = {*calendar.sessions.sessions, *calendar.sessions.overlaps}
        unknown = sorted(set(allowed) - known)
        if unknown or not allowed:
            raise ValueError(f"allowed sessions must be configured sessions, got {unknown}")
        self.calendar = calendar
        self.allowed = tuple(allowed)

    def check(self, ctx: FilterContext) -> str | None:
        """Blocked outside every allowed session."""
        if any(self.calendar.in_session(ctx.ts, name) for name in self.allowed):
            return None
        return f"outside the allowed sessions {list(self.allowed)}"


class BlackoutFilter:
    """The decision time lies in none of the event `windows`."""

    name = "blackout"
    pass_label = "pass"

    def __init__(self, calendar: CalendarLookup, windows: Sequence[str]) -> None:
        unknown = sorted(set(windows) - set(calendar.sessions.event_windows))
        if unknown:
            raise ValueError(f"blackouts must be configured event windows, got {unknown}")
        self.calendar = calendar
        self.windows = tuple(windows)

    def check(self, ctx: FilterContext) -> str | None:
        """Blocked inside a listed window."""
        for name in self.windows:
            if self.calendar.in_window(ctx.ts, name):
                return f"inside the {name} window"
        return None


class VolatilityBandFilter:
    """Daily sigma-hat within ``[low, high]`` (fractions of price)."""

    name = "volatility"
    pass_label = "pass"

    def __init__(self, low: float, high: float) -> None:
        if not 0 <= low < high:
            raise ValueError("the volatility band needs 0 <= low < high")
        self.low, self.high = low, high

    def check(self, ctx: FilterContext) -> str | None:
        """Blocked outside the band or without a sigma-hat."""
        sigma = ctx.sigma_daily
        if sigma is None:
            return "no sigma-hat at the decision"
        if not self.low <= sigma <= self.high:
            return f"sigma-hat {sigma:.4%} outside [{self.low:.4%}, {self.high:.4%}]"
        return None


class HourOfWeekSpreads:
    """Spreads observed so far, by New York hour of week (module docstring)."""

    def __init__(self, min_obs: int) -> None:
        if min_obs < 1:
            raise ValueError("min_obs must be positive")
        self.min_obs = min_obs
        self._buckets: defaultdict[int, list[float]] = defaultdict(list)
        self._all: list[float] = []

    @staticmethod
    def bucket(ts: int) -> int:
        """Hour of the week in New York (0 = Monday 00:00-01:00)."""
        local = pd.Timestamp(ts, tz="UTC").tz_convert(_NEW_YORK)
        return local.dayofweek * 24 + local.hour

    def observe(self, ts: int, spread: float) -> None:
        """A spread observed at `ts` (known from then on)."""
        self._buckets[self.bucket(ts)].append(spread)
        self._all.append(spread)

    def reference(self, ts: int) -> float | None:
        """The hour-of-week median at `ts`, the overall median as a fallback, or None."""
        values = self._buckets.get(self.bucket(ts), [])
        if len(values) >= self.min_obs:
            return float(statistics.median(values))
        if len(self._all) >= self.min_obs:
            return float(statistics.median(self._all))
        return None


class SpreadFilter:
    """The current spread is below `k` times its hour-of-week median."""

    name = "spread"
    pass_label = "pass"

    def __init__(self, k: float, history: HourOfWeekSpreads) -> None:
        if k <= 0:
            raise ValueError("k must be positive")
        self.k = k
        self.history = history

    def check(self, ctx: FilterContext) -> str | None:
        """Blocked at or above the multiple, or without a reference."""
        if ctx.spread is None:
            return "no quote at the decision"
        reference = self.history.reference(ctx.ts)
        if reference is None:
            return "no hour-of-week spread reference yet"
        if not ctx.spread < self.k * reference:
            return f"spread {ctx.spread:g} not below {self.k:g} x the median {reference:g}"
        return None


def _times(value: object) -> list[time]:
    """Every clock time inside a dumped configuration (nested dicts and lists)."""
    if isinstance(value, time):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _times(v)]
    if isinstance(value, list | tuple):
        return [t for v in value for t in _times(v)]
    return []
