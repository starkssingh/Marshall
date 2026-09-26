"""Helpers shared by the built-in checks. Registers no checks."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.data.flags import TickFlag
from xq.quality.registry import Anomaly, PartitionData

#: Flags that make a tick unusable as a price (mirrors the default bar exclusions).
UNUSABLE = TickFlag.MISSING_QUOTE | TickFlag.NONPOSITIVE | TickFlag.CROSSED | TickFlag.DUP_EXACT


def flag_mask(ticks: pd.DataFrame, flags: int) -> npt.NDArray[np.bool_]:
    """Rows carrying any of `flags`."""
    mask: npt.NDArray[np.bool_] = (ticks["flags"].to_numpy(dtype=np.uint32) & np.uint32(flags)) != 0
    return mask


def utc(ns: int | np.integer) -> pd.Timestamp:
    return pd.Timestamp(int(ns), unit="ns", tz="UTC")


def tick_anomalies(ticks: pd.DataFrame, mask: npt.NDArray[np.bool_], note: str) -> list[Anomaly]:
    """One anomaly per flagged tick, valued by its spread (so the widest come first)."""
    rows = ticks.loc[mask]
    spread = (rows["ask"] - rows["bid"]).to_numpy(dtype=np.float64)
    return [
        Anomaly(utc(ts), float(np.nan_to_num(s, nan=0.0)), note)
        for ts, s in zip(rows["ts_utc"].to_numpy(), spread, strict=True)
    ]


def windows(data: PartitionData, sessions: Iterable[str]) -> list[tuple[int, int]]:
    """UTC-ns union of the named sessions' windows on this trading day (empty if closed)."""
    intervals = []
    for name in sessions:
        opens, closes = data.day.get(f"{name}_open_utc"), data.day.get(f"{name}_close_utc")
        if opens is None or closes is None or pd.isna(opens) or pd.isna(closes):
            continue
        intervals.append((int(pd.Timestamp(opens).value), int(pd.Timestamp(closes).value)))
    intervals.sort()
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def market_hours(data: PartitionData) -> tuple[int, int] | None:
    """The day's market hours in UTC ns, or None when the market is closed."""
    opens, closes = data.day["market_open_utc"], data.day["market_close_utc"]
    if pd.isna(opens) or pd.isna(closes):
        return None
    return int(pd.Timestamp(opens).value), int(pd.Timestamp(closes).value)
