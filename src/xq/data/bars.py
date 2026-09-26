"""Bid, ask and mid bars on seven timeframes (DATA-008).

A bar covers the half-open interval ``[bar_start, bar_end)`` and is labelled by its start. It
carries ``available_at = bar_end + publication_latency``: nothing may use it earlier. Timeframes
up to 1h are aligned to UTC; 4h and 1d bars are aligned to the trading day, which starts at 17:00
New York (ADR 0007). Every timeframe is built directly from clean ticks, so every statistic —
including the spread median — is exact; a test proves that aggregating 1m bars reproduces the
aggregable fields of every higher timeframe.

Ticks whose flags intersect ``bars.exclude_flags`` do not enter bar prices; they are counted in
``n_excluded``. Included ticks that carry any flag are counted in ``n_flagged``. A minute without
included ticks produces no bar (never a forward-filled one) and becomes part of a gap in
``bar_gaps``. A bar is complete once the source's data reaches its end.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from xq.core.config import AppConfig, BarsConfig
from xq.core.errors import XQError
from xq.core.logging import get_logger
from xq.core.time import from_ns, to_ns, trading_day_bounds, trading_days
from xq.core.types import Timeframe
from xq.data.clean import clean_partition_path, rules_version
from xq.data.flags import TickFlag
from xq.data.raw_store import sha256_file
from xq.data.sessions import build_session_table
from xq.tracking.db import session_factory
from xq.tracking.models import BarGap, BarSet, CleanPartition, RawFile

#: Bump when bar logic changes; it is part of every build version hash.
BAR_CODE_VERSION = 1
BARS_DIR = "bars"
BASES = ("bid", "ask", "mid")
BASIS_LABEL = "bid+ask+mid"
OHLC = ("open", "high", "low", "close")
BUILD_VERSION_KEY = b"xq.build_version"

#: Bar columns and dtypes. Instants are UTC int64 nanoseconds; ``trading_day`` is a date.
BAR_SCHEMA: dict[str, str] = {
    "bar_start_utc": "int64",
    "available_at_utc": "int64",
    **{f"{basis}_{part}": "float64" for basis in BASES for part in OHLC},
    "tick_count": "int64",
    "spread_mean": "float64",
    "spread_med": "float64",
    "spread_max": "float64",
    "spread_close": "float64",
    "n_flagged": "int64",
    "n_excluded": "int64",
    "trading_day": "object",
    "is_complete": "bool",
}

log = get_logger(__name__)


class NoCleanDataError(XQError):
    """Bars were requested but no clean partitions exist for the configured rules version."""


@dataclass
class BarBuildResult:
    """What one `build_bar_sets` call did."""

    build_version: str
    clean_rules_version: str
    months: list[str] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)


def exclude_mask(cfg: BarsConfig) -> int:
    """The flag bits whose ticks stay out of bar prices."""
    mask = 0
    for name in cfg.exclude_flags:
        mask |= TickFlag[name]
    return mask


def build_version(cfg: BarsConfig, clean_rules_version: str) -> str:
    """``<label>-<hash>`` over the bar settings, `BAR_CODE_VERSION` and the clean rules version."""
    payload = json.dumps(
        {
            "code": BAR_CODE_VERSION,
            "bars": cfg.model_dump(mode="json"),
            "clean": clean_rules_version,
        },
        sort_keys=True,
    )
    return f"{cfg.version}-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"


def build_bars(
    ticks: pd.DataFrame,
    tf: Timeframe,
    *,
    exclude_flags: int,
    latency_ns: int,
    coverage_end_ns: int,
) -> pd.DataFrame:
    """Build bars of timeframe `tf` from canonical ticks sorted by ``ts_utc``.

    Args:
        ticks: Clean canonical ticks (any number of trading days), sorted by ``ts_utc``.
        tf: Timeframe to build.
        exclude_flags: Flag bits whose ticks do not enter prices (see `exclude_mask`).
        latency_ns: Publication latency added to each bar's end to give ``available_at_utc``.
        coverage_end_ns: The latest instant the source's data reaches; bars ending later are
            marked incomplete.
    """
    ts_all = ticks["ts_utc"].to_numpy(dtype=np.int64)
    if len(ts_all) and np.any(np.diff(ts_all) < 0):
        raise ValueError("ticks must be sorted by ts_utc")
    flags_all = ticks["flags"].to_numpy(dtype=np.uint32)
    excluded = (flags_all & np.uint32(exclude_flags)) != 0

    starts_all, ends_all = _bucket(ts_all, tf)
    excluded_starts, excluded_counts = np.unique(starts_all[excluded], return_counts=True)

    keep = ~excluded
    ts = ts_all[keep]
    if len(ts) == 0:
        return _empty_bars()
    bid = ticks["bid"].to_numpy(dtype=np.float64)[keep]
    ask = ticks["ask"].to_numpy(dtype=np.float64)[keep]
    flags = flags_all[keep]
    starts, ends = starts_all[keep], ends_all[keep]
    mid = (bid + ask) / 2
    spread = ask - bid

    first = np.flatnonzero(np.concatenate(([True], starts[1:] != starts[:-1])))
    last = np.concatenate((first[1:], [len(ts)])) - 1
    counts = last - first + 1
    group = np.repeat(np.arange(len(first)), counts)

    bars: dict[str, object] = {
        "bar_start_utc": starts[first],
        "available_at_utc": ends[first] + latency_ns,
    }
    for basis, values in (("bid", bid), ("ask", ask), ("mid", mid)):
        bars[f"{basis}_open"] = values[first]
        bars[f"{basis}_high"] = np.maximum.reduceat(values, first)
        bars[f"{basis}_low"] = np.minimum.reduceat(values, first)
        bars[f"{basis}_close"] = values[last]
    bars["tick_count"] = counts.astype(np.int64)
    bars["spread_mean"] = np.add.reduceat(spread, first) / counts
    bars["spread_med"] = pd.Series(spread).groupby(group).median().to_numpy()
    bars["spread_max"] = np.maximum.reduceat(spread, first)
    bars["spread_close"] = spread[last]
    bars["n_flagged"] = np.add.reduceat((flags != 0).astype(np.int64), first)
    n_excluded = (
        pd.Series(excluded_counts, index=excluded_starts, dtype=np.int64)
        .reindex(starts[first], fill_value=0)
        .to_numpy(dtype=np.int64)
    )
    bars["n_excluded"] = n_excluded
    bars["trading_day"] = [d.item() for d in trading_days(_utc_index(starts[first]))]
    bars["is_complete"] = ends[first] <= coverage_end_ns
    return pd.DataFrame(bars).astype(BAR_SCHEMA)


def build_bar_sets(
    cfg: AppConfig,
    engine: Engine,
    source_id: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> BarBuildResult:
    """Build all seven timeframes for the source's clean partitions (optionally within dates).

    Output: ``data/bars/<source>/<instrument>/tf=<tf>/build=<version>/year=YYYY/month=MM/
    part.parquet`` (month of the trading day), one ``bar_sets`` row per timeframe and the gaps of
    each timeframe in ``bar_gaps``. Rebuilding the same inputs reproduces the same bytes.
    """
    source = cfg.source(source_id)
    bars_cfg = cfg.bars_config()
    clean_version = rules_version(cfg.cleaning_config())
    version = build_version(bars_cfg, clean_version)
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)
    root = data_dir / BARS_DIR / source_id / source.instrument
    result = BarBuildResult(build_version=version, clean_rules_version=clean_version)

    with session_factory(engine)() as session:
        days = sorted(
            session.scalars(
                select(CleanPartition.trading_day).where(
                    CleanPartition.source_id == source_id,
                    CleanPartition.rules_version == clean_version,
                )
            )
        )
        days = [d for d in days if (start is None or d >= start) and (end is None or d <= end)]
        if not days:
            raise NoCleanDataError(
                f"no clean partitions for {source_id!r} with rules {clean_version}; "
                "run `xq clean` first"
            )
        coverage_end = max(
            to_ns(ts)
            for ts in session.scalars(
                select(RawFile.last_ts_utc).where(RawFile.source_id == source_id)
            )
            if ts is not None
        )
        mask = exclude_mask(bars_cfg)
        latency = bars_cfg.publication_latency_ms * 1_000_000

        for month, month_days in _by_month(days).items():
            ticks = pd.concat(
                [
                    pd.read_parquet(clean_partition_path(cfg, source_id, day, clean_version))
                    for day in month_days
                ],
                ignore_index=True,
            ).sort_values("ts_utc", kind="stable")
            for tf in Timeframe:
                bars = build_bars(
                    ticks, tf, exclude_flags=mask, latency_ns=latency, coverage_end_ns=coverage_end
                )
                _write_bars(bars, _month_path(root, tf, version, month), version)
            result.months.append(month)
            log.info("bars_built", month=month, ticks=len(ticks))

        calendar = build_session_table(cfg.sessions_config(), days[0], days[-1])
        for tf in Timeframe:
            result.rows[tf.value] = _record_bar_set(
                session, root, source_id, source.instrument, tf, version, clean_version, calendar
            )
        session.commit()
    return result


def bar_set_id(source_id: str, instrument_id: str, tf: Timeframe, version: str) -> str:
    """Identifier of the bar set for one timeframe and build version."""
    return f"{source_id}:{instrument_id}:{tf.value}:{version}"


def bar_set_dir(cfg: AppConfig, source_id: str, tf: Timeframe, version: str) -> Path:
    """Directory holding the monthly files of one bar set."""
    source = cfg.source(source_id)
    root = cfg.paths.resolve(cfg.paths.data_dir) / BARS_DIR / source_id / source.instrument
    return root / f"tf={tf.value}" / f"build={version}"


def find_gaps(
    bars: pd.DataFrame,
    tf: Timeframe,
    market_open: npt.NDArray[np.int64],
    market_close: npt.NDArray[np.int64],
) -> pd.DataFrame:
    """Intervals between consecutive bars; ``expected_open`` if any part lies in market hours.

    `market_open`/`market_close` are sorted, non-overlapping market-hours intervals (UTC ns).
    """
    starts = bars["bar_start_utc"].to_numpy(dtype=np.int64)
    if len(starts) < 2:
        return pd.DataFrame({"gap_start_utc": [], "gap_end_utc": [], "expected_open": []})
    _, ends = _bucket(starts, tf)
    gap_start, gap_end = ends[:-1], starts[1:]
    has_gap = gap_end > gap_start
    gap_start, gap_end = gap_start[has_gap], gap_end[has_gap]
    index = np.searchsorted(market_close, gap_start, side="right")
    inside = index < len(market_open)
    expected = np.zeros(len(gap_start), dtype=bool)
    expected[inside] = market_open[index[inside]] < gap_end[inside]
    return pd.DataFrame(
        {"gap_start_utc": gap_start, "gap_end_utc": gap_end, "expected_open": expected}
    )


def _bucket(
    ts: npt.NDArray[np.int64], tf: Timeframe
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    """Start and end of the bar each instant falls in."""
    if not tf.anchored_to_trading_day:
        starts = ts - np.mod(ts, tf.nanos)
        return starts, starts + tf.nanos
    days = trading_days(_utc_index(ts))
    unique_days, inverse = np.unique(days, return_inverse=True)
    bounds = [trading_day_bounds(d.item()) for d in unique_days]
    day_start = np.array([to_ns(b[0]) for b in bounds], dtype=np.int64)[inverse]
    day_end = np.array([to_ns(b[1]) for b in bounds], dtype=np.int64)[inverse]
    starts = day_start + (ts - day_start) // tf.nanos * tf.nanos
    return starts, np.minimum(starts + tf.nanos, day_end)


def _utc_index(ns: npt.NDArray[np.int64]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(ns, unit="ns", utc=True))


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=d) for c, d in BAR_SCHEMA.items()})


def _by_month(days: list[date]) -> dict[str, list[date]]:
    months: dict[str, list[date]] = {}
    for day in days:
        months.setdefault(f"{day.year:04d}-{day.month:02d}", []).append(day)
    return months


def _month_path(root: Path, tf: Timeframe, version: str, month: str) -> Path:
    year, month_number = month.split("-")
    return (
        root
        / f"tf={tf.value}"
        / f"build={version}"
        / f"year={year}"
        / f"month={month_number}"
        / "part.parquet"
    )


def _write_bars(bars: pd.DataFrame, target: Path, version: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(bars, preserve_index=False)
    table = table.replace_schema_metadata(
        {**(table.schema.metadata or {}), BUILD_VERSION_KEY: version.encode()}
    )
    temporary = target.with_suffix(".parquet.tmp")
    pq.write_table(table, temporary, compression="zstd")
    os.replace(temporary, target)


def _record_bar_set(
    session: Session,
    root: Path,
    source_id: str,
    instrument_id: str,
    tf: Timeframe,
    version: str,
    clean_version: str,
    calendar: pd.DataFrame,
) -> int:
    files = sorted((root / f"tf={tf.value}" / f"build={version}").rglob("part.parquet"))
    bars = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    digest = hashlib.sha256("".join(sha256_file(f) for f in files).encode()).hexdigest()
    set_id = bar_set_id(source_id, instrument_id, tf, version)
    starts = bars["bar_start_utc"].to_numpy(dtype=np.int64)
    ends = bars["available_at_utc"].to_numpy(dtype=np.int64)
    values = {
        "source_id": source_id,
        "instrument_id": instrument_id,
        "timeframe": tf.value,
        "basis": BASIS_LABEL,
        "clean_rules_version": clean_version,
        "build_version": version,
        "start_utc": from_ns(int(starts.min())),
        "end_utc": from_ns(int(ends.max())),
        "row_count": len(bars),
        "sha256": digest,
    }
    existing = session.get(BarSet, set_id)
    if existing is None:
        session.add(BarSet(bar_set_id=set_id, **values))
    else:
        for key, value in values.items():
            setattr(existing, key, value)
    session.flush()

    open_days = calendar[calendar["is_open"]]
    gaps = find_gaps(
        bars,
        tf,
        open_days["market_open_utc"].to_numpy(dtype="datetime64[ns]").view(np.int64),
        open_days["market_close_utc"].to_numpy(dtype="datetime64[ns]").view(np.int64),
    )
    session.execute(delete(BarGap).where(BarGap.bar_set_id == set_id))
    session.add_all(
        BarGap(
            bar_set_id=set_id,
            gap_start_utc=from_ns(int(g_start)),
            gap_end_utc=from_ns(int(g_end)),
            expected_open=bool(expected),
        )
        for g_start, g_end, expected in zip(
            gaps["gap_start_utc"], gaps["gap_end_utc"], gaps["expected_open"], strict=True
        )
    )
    return len(bars)
