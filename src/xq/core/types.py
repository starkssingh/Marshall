"""Core enumerations shared by every layer (ARCH-005)."""

from __future__ import annotations

from enum import StrEnum

import pandas as pd


class Timeframe(StrEnum):
    """Bar timeframes. Values are the canonical short names used in paths and configs."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def nanos(self) -> int:
        """Bar length in nanoseconds."""
        return _TIMEFRAME_NANOS[self]

    @property
    def duration(self) -> pd.Timedelta:
        """Bar length as a timedelta."""
        return pd.Timedelta(self.nanos, unit="ns")

    @property
    def anchored_to_trading_day(self) -> bool:
        """True if bars align to the 17:00 New York trading-day start rather than to UTC."""
        return self in (Timeframe.H4, Timeframe.D1)


_MINUTE_NS = 60 * 1_000_000_000
_TIMEFRAME_NANOS: dict[Timeframe, int] = {
    Timeframe.M1: _MINUTE_NS,
    Timeframe.M5: 5 * _MINUTE_NS,
    Timeframe.M15: 15 * _MINUTE_NS,
    Timeframe.M30: 30 * _MINUTE_NS,
    Timeframe.H1: 60 * _MINUTE_NS,
    Timeframe.H4: 240 * _MINUTE_NS,
    Timeframe.D1: 1440 * _MINUTE_NS,
}


class PriceBasis(StrEnum):
    """Which quote a price series is built from."""

    BID = "bid"
    ASK = "ask"
    MID = "mid"


class Side(StrEnum):
    """Trade direction. Buys fill at the ask and sells at the bid (never at mid)."""

    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for buy, -1 for sell."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        """The side that closes a position opened on this side."""
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def fill_basis(self) -> PriceBasis:
        """The quote an order on this side executes against."""
        return PriceBasis.ASK if self is Side.BUY else PriceBasis.BID
