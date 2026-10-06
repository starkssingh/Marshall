"""Tick-level data-quality checks (DQ-002).

Each check reads the clean ticks of one trading day, including flagged rows, and reports a number
where larger is worse. Where cleaning may drop rows (exact duplicates, non-positive and crossed
quotes), the dropped counts are added back so a configured drop cannot hide a defect.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from xq.data.flags import TickFlag
from xq.data.spreads import hour_of_week
from xq.quality.checks.common import (
    UNUSABLE,
    flag_mask,
    market_hours,
    tick_anomalies,
    utc,
    windows,
)
from xq.quality.registry import Anomaly, Measurement, PartitionData, Scope, register

_TIMESTAMP_FLAGS = (
    TickFlag.TS_OUT_OF_ORDER | TickFlag.TS_DST_AMBIGUOUS | TickFlag.TS_DST_NONEXISTENT
)
_HOUR_NS = 3_600 * 1_000_000_000


def _flag_fraction(
    data: PartitionData, flags: int, dropped_rules: tuple[str, ...], note: str
) -> Measurement | None:
    ticks = data.ticks
    dropped = sum(data.dropped.get(rule, 0) for rule in dropped_rules)
    total = len(ticks) + dropped
    if total == 0:
        return None
    mask = flag_mask(ticks, flags)
    hits = int(mask.sum()) + dropped
    return Measurement(
        hits / total,
        {"ticks": total, "hits": hits, "dropped": dropped},
        tick_anomalies(ticks, mask, note),
    )


@register
class TimestampOrdering:
    """Share of ticks whose timestamps were out of order or DST-ambiguous/nonexistent."""

    check_id = "tick.ordering"
    scope = Scope.TICK
    description = "ticks out of order in their file, or at DST-ambiguous/nonexistent local times"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        return _flag_fraction(data, _TIMESTAMP_FLAGS, (), "timestamp flag")


@register
class ExactDuplicates:
    """Share of ticks repeating an earlier tick's timestamp and quote (overlapping exports)."""

    check_id = "tick.duplicates_exact"
    scope = Scope.TICK
    description = "ticks with the same timestamp and quote as an earlier tick"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        return _flag_fraction(data, TickFlag.DUP_EXACT, ("DUP_EXACT",), "exact duplicate")


@register
class SameTimeOtherPrice:
    """Share of ticks sharing an earlier tick's timestamp with a different quote."""

    check_id = "tick.duplicates_diff_price"
    scope = Scope.TICK
    description = "ticks with the same timestamp as an earlier tick but a different quote"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        return _flag_fraction(data, TickFlag.DUP_TS_DIFF_PRICE, (), "same time, other price")


@register
class NonPositiveOrCrossed:
    """Share of ticks with a non-positive or crossed quote."""

    check_id = "tick.nonpositive_crossed"
    scope = Scope.TICK
    description = "ticks with bid or ask <= 0, or bid > ask"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        return _flag_fraction(
            data,
            TickFlag.NONPOSITIVE | TickFlag.CROSSED,
            ("NONPOSITIVE", "CROSSED"),
            "non-positive or crossed",
        )


@register
class SpreadOutliers:
    """Share of usable ticks whose spread exceeds `multiple` x the p50 of their hour of week."""

    check_id = "tick.spread_outliers"
    scope = Scope.TICK
    description = "ticks with spread above a multiple of their New York hour-of-week median"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        if data.spread_stats is None or data.spread_stats.empty:
            return None
        ticks = data.ticks.loc[~flag_mask(data.ticks, UNUSABLE)]
        if ticks.empty:
            return None
        median = data.spread_stats.set_index("hour_of_week")["p50"]
        hours = hour_of_week(ticks["ts_utc"].to_numpy(dtype=np.int64))
        reference = median.reindex(hours).to_numpy(dtype=np.float64)
        spread = (ticks["ask"] - ticks["bid"]).to_numpy(dtype=np.float64)
        known = ~np.isnan(reference) & (reference > 0)
        with np.errstate(invalid="ignore"):
            outlier = known & (spread > float(params["multiple"]) * reference)
        rows = ticks.loc[outlier]
        anomalies = [
            Anomaly(utc(ts), float(s / r), f"spread {s:.2f} vs hour-of-week p50 {r:.2f}")
            for ts, s, r in zip(
                rows["ts_utc"].to_numpy(), spread[outlier], reference[outlier], strict=True
            )
        ]
        return Measurement(
            float(outlier.sum()) / int(known.sum()) if known.any() else 0.0,
            {"ticks_compared": int(known.sum()), "outliers": int(outlier.sum())},
            anomalies,
        )


@register
class RevertingSpikes:
    """Reverting spike events (runs of consecutive SPIKE-flagged ticks) per million usable ticks.

    The rate, not the count, is graded (ADR 0069 item 1): the feed's tick rate tripled over the
    history, so the same daily count means something different in different years.
    """

    check_id = "tick.spikes"
    scope = Scope.TICK
    description = "reverting whole-quote spikes per million usable ticks"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        usable = int((~flag_mask(data.ticks, UNUSABLE)).sum())
        if usable == 0:
            return None
        mask = flag_mask(data.ticks, TickFlag.SPIKE)
        starts = mask & ~np.concatenate(([False], mask[:-1]))
        events = int(starts.sum())
        mid = ((data.ticks["bid"] + data.ticks["ask"]) / 2).to_numpy(dtype=np.float64)
        anomalies = [
            Anomaly(utc(data.ticks["ts_utc"].iloc[i]), float(mid[i] - mid[i - 1]), "spike start")
            for i in np.flatnonzero(starts)
            if i > 0
        ]
        return Measurement(
            events * 1e6 / usable,
            {"events": events, "ticks": int(mask.sum()), "usable_ticks": usable},
            anomalies,
        )


@register
class StaleQuotes:
    """Seconds within the active sessions during which ticks kept arriving with an unchanged quote.

    Only time covered by ticks counts (ADR 0069 item 3): the interval between two consecutive
    usable ticks belongs to the quote of the first, unless the ticks are more than `stale_seconds`
    apart — then nothing arrived at all, which is missing data (``bar.missing_minutes``,
    ``cal.missing_open_data``), not a frozen quote. A run of one quote counts when its covered time
    inside a session exceeds `stale_seconds`.
    """

    check_id = "tick.stale_quotes"
    scope = Scope.TICK
    description = "time in active sessions during which ticks repeated an unchanged quote too long"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        sessions = windows(data, params["active_sessions"])
        if not sessions:
            return None
        limit_ns = int(float(params["stale_seconds"]) * 1e9)
        ticks = data.ticks.loc[~flag_mask(data.ticks, UNUSABLE)]
        ts = ticks["ts_utc"].to_numpy(dtype=np.int64)
        bid = ticks["bid"].to_numpy(dtype=np.float64)
        ask = ticks["ask"].to_numpy(dtype=np.float64)
        changed = np.ones(len(ts), dtype=bool)
        changed[1:] = (bid[1:] != bid[:-1]) | (ask[1:] != ask[:-1])
        run = np.cumsum(changed) - 1  # the quote run each tick belongs to
        # Interval i is [ts[i], ts[i + 1]): covered by run[i] unless it is a silence.
        begins, ends = ts[:-1], ts[1:]
        covered = (ends - begins) <= limit_ns

        stale_ns = 0
        anomalies: list[Anomaly] = []
        for start, end in sessions:
            lo = np.clip(begins[covered], start, end)
            hi = np.clip(ends[covered], start, end)
            inside = hi > lo
            if not inside.any():
                continue
            runs = run[:-1][covered][inside]
            lengths = np.bincount(runs, weights=(hi - lo)[inside]).astype(np.int64)
            first = np.full(len(lengths), np.iinfo(np.int64).max, dtype=np.int64)
            np.minimum.at(first, runs, lo[inside])
            for r in np.flatnonzero(lengths > limit_ns):
                stale_ns += int(lengths[r])
                anomalies.append(
                    Anomaly(utc(first[r]), lengths[r] / 1e9, "seconds of ticks without change")
                )
        return Measurement(stale_ns / 1e9, {"sessions": list(params["active_sessions"])}, anomalies)


@register
class TickRateAnomalies:
    """Market hours whose tick count is far from the norm for that New York hour of week."""

    check_id = "tick.rate_anomalies"
    scope = Scope.TICK
    description = "full market hours with tick counts far below or above their hour-of-week norm"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        hours = market_hours(data)
        norm = data.hourly_tick_norm
        if hours is None or norm is None or norm.empty:
            return None
        opens, closes = hours
        if data.coverage is not None:  # hours the source's data does not reach are not quiet
            opens, closes = max(opens, data.coverage[0]), min(closes, data.coverage[1] + 1)
        first_hour = -(-opens // _HOUR_NS) * _HOUR_NS  # first whole UTC hour inside the market
        starts = np.arange(first_hour, closes - _HOUR_NS + 1, _HOUR_NS, dtype=np.int64)
        if len(starts) == 0:
            return None
        ts = data.ticks["ts_utc"].to_numpy(dtype=np.int64)
        counts = np.histogram(ts, bins=np.append(starts, starts[-1] + _HOUR_NS))[0]
        expected = norm.reindex(hour_of_week(starts)).to_numpy(dtype=np.float64)
        known = ~np.isnan(expected) & (expected > 0)
        low = known & (counts < float(params["low_ratio"]) * expected)
        high = known & (counts > float(params["high_ratio"]) * expected)
        anomalous = low | high
        anomalies = [
            Anomaly(utc(s), float(c / e), "tick count / hour-of-week norm")
            for s, c, e in zip(
                starts[anomalous], counts[anomalous], expected[anomalous], strict=True
            )
        ]
        return Measurement(
            float(anomalous.sum()),
            {"hours_compared": int(known.sum()), "low": int(low.sum()), "high": int(high.sum())},
            anomalies,
        )


def hourly_tick_counts(ts_utc: np.ndarray, market: list[tuple[int, int]]) -> pd.Series:
    """Tick counts per whole UTC hour inside the given market-hours intervals, by hour of week.

    Used to build `PartitionData.hourly_tick_norm` (the median over weeks) from history.
    """
    starts_all: list[np.ndarray] = []
    for opens, closes in market:
        first = -(-opens // _HOUR_NS) * _HOUR_NS
        starts_all.append(np.arange(first, closes - _HOUR_NS + 1, _HOUR_NS, dtype=np.int64))
    if not starts_all or not sum(len(s) for s in starts_all):
        return pd.Series(dtype=np.float64)
    starts = np.concatenate(starts_all)
    index = np.searchsorted(starts, ts_utc, side="right") - 1
    inside = (index >= 0) & (ts_utc < starts[np.maximum(index, 0)] + _HOUR_NS)
    counts = np.bincount(index[inside], minlength=len(starts))
    return pd.Series(counts, index=hour_of_week(starts), dtype=np.float64)
