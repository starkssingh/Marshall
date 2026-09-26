"""Dense synthetic canonical ticks and defect injectors for cleaning and quality tests.

`dense_ticks` produces clean canonical ticks (``TICK_SCHEMA``) from the same broker-hours model as
`helpers.mt5_fixtures`, with every row changing the quote. The injectors add or alter rows so a
test knows exactly which ticks must be flagged: injected rows carry ``row_num`` values from
`INJECTED` upwards, altered rows keep their ``row_num``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from helpers.mt5_fixtures import BOTH, synthetic_ticks, to_mt5_text
from xq.data.adapters.base import TICK_SCHEMA

INJECTED = 1_000_000
SECOND_NS = 1_000_000_000


def dense_ticks(
    start_utc: str,
    end_utc: str,
    *,
    seed: int = 7,
    mean_interval_s: float = 5.0,
    start_price: float = 2150.0,
    raw_file_id: str = "synthetic",
) -> pd.DataFrame:
    """Clean canonical ticks in ``[start_utc, end_utc)`` during broker market hours."""
    ticks = synthetic_ticks(
        start_utc, end_utc, seed=seed, start_price=start_price, mean_interval_s=mean_interval_s
    )
    bid = ticks["bid"].to_numpy(dtype=np.float64)
    ask = ticks["ask"].to_numpy(dtype=np.float64)
    # Make every row a quote change, so nothing is stale or duplicated by construction.
    unchanged = np.zeros(len(bid), dtype=bool)
    unchanged[1:] = (bid[1:] == bid[:-1]) & (ask[1:] == ask[:-1])
    ask[unchanged] += 0.01
    frame = pd.DataFrame(
        {
            "ts_utc": ticks["ts_utc"].to_numpy(dtype="datetime64[ns]").view(np.int64),
            "bid": bid,
            "ask": ask,
            "bid_size": np.nan,
            "ask_size": np.nan,
            "flags": np.zeros(len(bid), dtype=np.uint32),
            "raw_file_id": raw_file_id,
            "row_num": np.arange(len(bid), dtype=np.int64),
        }
    )
    return frame.astype(TICK_SCHEMA)


def between(frame: pd.DataFrame, i: int) -> int:
    """A timestamp strictly between rows `i` and `i + 1`."""
    t0, t1 = int(frame["ts_utc"].iloc[i]), int(frame["ts_utc"].iloc[i + 1])
    assert t1 - t0 >= 2, "rows too close to insert between"
    return t0 + (t1 - t0) // 2


def row_like(frame: pd.DataFrame, i: int, **changes: Any) -> dict[str, Any]:
    """A copy of row `i` with `changes` applied."""
    row = frame.iloc[i].to_dict()
    row.update(changes)
    return row


def inject(frame: pd.DataFrame, rows: list[dict[str, Any]]) -> tuple[pd.DataFrame, list[int]]:
    """Append `rows` with fresh ``row_num`` ids, re-sort; return the frame and the new ids."""
    start = INJECTED + int((frame["row_num"] >= INJECTED).sum())
    ids = list(range(start, start + len(rows)))
    added = pd.DataFrame([{**row, "row_num": rid} for row, rid in zip(rows, ids, strict=True)])
    combined = pd.concat([frame, added.astype(TICK_SCHEMA)], ignore_index=True)
    combined = combined.sort_values(["ts_utc", "raw_file_id", "row_num"], kind="stable")
    return combined.reset_index(drop=True).astype(TICK_SCHEMA), ids


def exact_duplicate(frame: pd.DataFrame, i: int) -> dict[str, Any]:
    return row_like(frame, i)


def same_time_other_price(frame: pd.DataFrame, i: int) -> dict[str, Any]:
    row = row_like(frame, i)
    return {**row, "bid": row["bid"] + 0.05, "ask": row["ask"] + 0.05}


def non_positive(frame: pd.DataFrame, i: int) -> dict[str, Any]:
    return row_like(frame, i, ts_utc=between(frame, i), bid=0.0)


def crossed(frame: pd.DataFrame, i: int) -> dict[str, Any]:
    row = row_like(frame, i, ts_utc=between(frame, i))
    return {**row, "bid": row["ask"] + 0.10}


def wide_spread(frame: pd.DataFrame, i: int, width: float = 6.0) -> dict[str, Any]:
    row = row_like(frame, i, ts_utc=between(frame, i))
    return {**row, "ask": row["bid"] + width}


def price_spike(frame: pd.DataFrame, i: int, jump: float = 4.0) -> dict[str, Any]:
    """One tick with the whole quote moved by `jump`; the next original tick reverts it."""
    row = row_like(frame, i, ts_utc=between(frame, i))
    return {**row, "bid": row["bid"] + jump, "ask": row["ask"] + jump}


def frozen_quote(
    frame: pd.DataFrame, i: int, *, seconds: int = 300, every: int = 10
) -> tuple[pd.DataFrame, list[int]]:
    """Replace the ticks after row `i` for `seconds` by repeats of row `i`'s quote.

    Returns the frame and the ids of repeats more than 120 s after row `i` (the stale ones).
    """
    t0 = int(frame["ts_utc"].iloc[i])
    window = (frame["ts_utc"] > t0) & (frame["ts_utc"] <= t0 + seconds * SECOND_NS)
    kept = frame.loc[~window].reset_index(drop=True)
    repeats = [
        row_like(frame, i, ts_utc=t0 + k * SECOND_NS) for k in range(every, seconds + 1, every)
    ]
    combined, ids = inject(kept, repeats)
    stale = [rid for rid, k in zip(ids, range(every, seconds + 1, every), strict=True) if k > 120]
    return combined, stale


def flagged_ids(frame: pd.DataFrame, flag: int) -> set[int]:
    """``row_num`` of rows carrying `flag`."""
    hit = (frame["flags"].to_numpy() & np.uint32(flag)) != 0
    return set(frame.loc[hit, "row_num"].tolist())


def widen_rollover(frame: pd.DataFrame, factor: float = 6.0) -> pd.DataFrame:
    """Widen spreads around the 17:00 New York rollover, as retail XAUUSD feeds do.

    Ticks in the ten minutes before the close (16:50-17:00) and the first half hour after the
    reopen (18:00-18:30 New York) get their ask moved up so the spread is `factor` times wider.
    """
    local = pd.DatetimeIndex(pd.to_datetime(frame["ts_utc"], unit="ns", utc=True)).tz_convert(
        "America/New_York"
    )
    minutes = np.asarray(local.hour * 60 + local.minute)
    around = ((minutes >= 16 * 60 + 50) & (minutes < 17 * 60)) | (
        (minutes >= 18 * 60) & (minutes < 18 * 60 + 30)
    )
    widened = frame.copy()
    spread = widened["ask"] - widened["bid"]
    widened.loc[around, "ask"] = np.round(widened.loc[around, "bid"] + spread[around] * factor, 2)
    return widened


def write_mt5(frame: pd.DataFrame, path: Path) -> Path:
    """Write canonical ticks as an MT5 tick export (both sides on every row, NY+7 server time)."""
    ticks = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(frame["ts_utc"], unit="ns", utc=True),
            "bid": frame["bid"],
            "ask": frame["ask"],
            "changed": BOTH,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_mt5_text(ticks), encoding="utf-8")
    return path
