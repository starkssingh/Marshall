"""Bit flags carried by every canonical tick (``flags`` column, uint32).

Flags describe; they never remove data. Bits 0-15 are set while reading and normalizing a source
(DATA-003, DATA-006); bits 16-31 are reserved for the versioned cleaning rules (DATA-007).
"""

from __future__ import annotations

from enum import IntFlag


class TickFlag(IntFlag):
    """Why a tick deserves attention. Combine with ``|``; test with ``&``."""

    NONE = 0
    #: Local time occurred twice (DST fall-back); resolved from file order.
    TS_DST_AMBIGUOUS = 1 << 0
    #: Local time does not exist (DST spring-forward); shifted to the transition instant.
    TS_DST_NONEXISTENT = 1 << 1
    #: Earlier than a preceding row of the same file after conversion to UTC.
    TS_OUT_OF_ORDER = 1 << 2
    #: Bid or ask unknown at this row (e.g. an MT5 file starting with a one-sided update).
    MISSING_QUOTE = 1 << 3

    # Cleaning rules (DATA-007, `xq.data.clean`).
    #: Same timestamp and quote as an earlier tick of the partition.
    DUP_EXACT = 1 << 16
    #: Same timestamp as an earlier tick, different quote.
    DUP_TS_DIFF_PRICE = 1 << 17
    #: Bid or ask is zero or negative.
    NONPOSITIVE = 1 << 18
    #: Bid above ask.
    CROSSED = 1 << 19
    #: Spread above a multiple of the trailing median spread.
    SPREAD_OUTLIER = 1 << 20
    #: Mid jump beyond a robust z threshold that reverts within a few ticks (uses later ticks).
    SPIKE = 1 << 21
    #: Outside the calendar's market hours.
    CLOSED_MARKET = 1 << 22
    #: Quote unchanged for longer than the stale limit.
    STALE = 1 << 23


FLAG_DTYPE = "uint32"
