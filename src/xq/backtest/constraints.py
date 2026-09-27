"""Session constraints of the event backtester: entry blackouts and the weekend exit (BT-008).

**Entry blackouts.** No order that opens, increases or flips exposure may fill inside a blackout.
Orders that only reduce exposure are never blocked, so a position can always be closed. The
blackouts are (``backtest.event.blackouts``):

- the configured event windows of ``config/sessions.yaml`` — by default ``rollover`` (16:45-18:15
  New York on every day, which also covers the Sunday reopen; C-3, ADR 0026) and
  ``us_data_release`` (from 5 minutes before to 30 minutes after each US data release);
- the last ``before_weekly_close_min`` minutes before a *weekly close*: a market close followed by
  at least 24 hours without trading (a weekend or a full-day holiday).

The engine refuses an intent that clearly opens or flips a position at a decision time inside a
blackout (before the risk decision; the ledger records the refusal). The broker applies the
same rule to the order itself when it arrives (market orders are rejected) and at every quote
(entries never fill in a blackout: a market entry is cancelled, a resting stop or limit entry
waits). A same-side intent that changes the size is decided and sized first; the broker then
knows whether its order increases the exposure.

**Flat before the weekend** (optional, ``flat_before_weekend``): the engine closes every position
``flat_before_weekend_min`` minutes before each weekly close, through the risk approver like any
other intent.

The windows are computed once as exact UTC intervals over the market clock's range — clock
windows per calendar day in their own time zone, anchor windows around the session table's
anchors — so they agree with the dataset calendar columns ``in_<name>_window`` (DS-007, tested)
and handle DST by construction.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import AppConfig, ClockWindow, EventBacktestConfig, EventWindow, SessionsConfig
from xq.core.errors import ConfigError
from xq.core.time import trading_day
from xq.data.calendar import MarketClock
from xq.data.sessions import build_session_table
from xq.signals.schema import TradeIntent

_MINUTE_NS = 60_000_000_000
#: A close followed by at least this long without trading is a weekly close.
WEEKLY_GAP_NS = 24 * 3_600_000_000_000
IntArray = npt.NDArray[np.int64]


class SessionConstraints:
    """Entry blackouts and weekend exits over the market clock's range (module docstring)."""

    def __init__(
        self, sessions: SessionsConfig, clock: MarketClock, config: EventBacktestConfig
    ) -> None:
        self.config = config
        self.clock = clock
        blackouts = config.blackouts
        unknown = sorted(set(blackouts.event_windows) - set(sessions.event_windows))
        if unknown:
            raise ConfigError(f"blackout windows {unknown} are not configured event windows")
        first = trading_day(pd.Timestamp(clock.covered_from, tz="UTC"))
        last = trading_day(pd.Timestamp(clock.covered_to - 1, tz="UTC"))
        self.windows: dict[str, tuple[IntArray, IntArray]] = {
            name: _window_intervals(sessions, name, first, last) for name in blackouts.event_windows
        }
        gaps = clock.opens[1:] - clock.closes[:-1]
        self.weekly_closes: IntArray = clock.closes[:-1][gaps >= WEEKLY_GAP_NS]
        self.before_close_ns = blackouts.before_weekly_close_min * _MINUTE_NS

    @classmethod
    def from_config(cls, cfg: AppConfig, clock: MarketClock) -> SessionConstraints:
        """The configured constraints (``backtest.event``)."""
        return cls(cfg.sessions_config(), clock, cfg.backtest_config().event_config())

    def entry_blackout(self, ts: int) -> str | None:
        """Why no entry may fill at `ts` (the blackout's name), or None."""
        for name, (starts, ends) in self.windows.items():
            k = int(np.searchsorted(starts, ts, side="right")) - 1
            if k >= 0 and ts < ends[k]:
                return f"{name} window"
        if self.before_close_ns:
            k = int(np.searchsorted(self.weekly_closes, ts, side="right"))
            if k < len(self.weekly_closes) and self.weekly_closes[k] - self.before_close_ns <= ts:
                minutes = self.config.blackouts.before_weekly_close_min
                return f"the last {minutes} min before the weekly close"
        return None

    def refuse(self, intent: TradeIntent, now: int, position_lots: float) -> str | None:
        """Refuse an intent that opens or flips a position inside a blackout."""
        if intent.direction == "flat":
            return None
        opens = position_lots == 0 or np.sign(position_lots) != intent.sign
        if not opens:
            return None
        reason = self.entry_blackout(now)
        return None if reason is None else f"entry blackout: {reason}"

    def flat_times(self, start: int, end: int) -> list[int]:
        """Instants in ``[start, end]`` at which positions are closed before a weekly close."""
        if not self.config.flat_before_weekend:
            return []
        lead = self.config.flat_before_weekend_min * _MINUTE_NS
        times = self.weekly_closes - lead
        return [int(t) for t in times if start <= t <= end]


def _window_intervals(
    sessions: SessionsConfig, name: str, first: date, last: date
) -> tuple[IntArray, IntArray]:
    """Sorted, merged UTC intervals ``[start, end)`` of event window `name` over the days."""
    window = sessions.event_windows[name]
    pairs: list[tuple[int, int]] = []
    if isinstance(window, ClockWindow):
        for offset in range((last - first).days + 3):
            day = first - timedelta(days=1) + timedelta(days=offset)
            pairs.append((_local(day, window.start, window.tz), _local(day, window.end, window.tz)))
    else:
        assert isinstance(window, EventWindow)
        table = build_session_table(sessions, first - timedelta(days=1), last + timedelta(days=1))
        anchors = pd.to_datetime(table[f"{name}_utc"], utc=True).dropna()
        for anchor in anchors:
            at = int(pd.Timestamp(anchor).value)
            pairs.append((at - window.before_min * _MINUTE_NS, at + window.after_min * _MINUTE_NS))
    pairs.sort()
    merged: list[list[int]] = []
    for start, end in pairs:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    starts = np.array([m[0] for m in merged], dtype=np.int64)
    ends = np.array([m[1] for m in merged], dtype=np.int64)
    return starts, ends


def _local(day: date, at: object, tz: str) -> int:
    """UTC nanoseconds of wall-clock time `at` on `day` in `tz` (DST gaps shift forward)."""
    wall = pd.Timestamp(f"{day.isoformat()} {at}")
    local = wall.tz_localize(tz, nonexistent="shift_forward", ambiguous=False)
    return int(local.tz_convert("UTC").value)
