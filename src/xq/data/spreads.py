"""Hour-of-week spread statistics (DATA-009).

For each source, the spread (ask - bid) of usable clean ticks is summarised per hour of the week in
New York local time — ``hour_of_week = weekday * 24 + hour`` with Monday 00:00 New York = 0 — so the
17:00 rollover always lands in the same buckets whatever the DST state. The p50, p90 and p99 feed
the cost-model fallback (BT-001) and the DQ spread check.

Prices sit on the instrument's tick grid, so spreads are counted exactly as integer multiples of
the tick size. Percentiles are therefore exact (the inverted-CDF definition: the smallest spread
with at least q of the ticks at or below it) and memory stays bounded however many ticks there
are.

Only data before ``vault.start`` is used: these statistics are research inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import numpy.typing as npt
import pandas as pd
from sqlalchemy import Engine, delete, select

from xq.core.config import AppConfig
from xq.core.errors import XQError
from xq.core.logging import get_logger
from xq.core.time import NEW_YORK, from_ns, to_ns, trading_day_bounds
from xq.data.bars import exclude_mask
from xq.data.clean import clean_partition_path, rules_version
from xq.tracking.db import session_factory
from xq.tracking.models import CleanPartition, SpreadStat

HOURS_PER_WEEK = 168
QUANTILES = {"p50": 0.50, "p90": 0.90, "p99": 0.99}

log = get_logger(__name__)


@dataclass(frozen=True)
class SpreadStatsResult:
    """What one `build_spread_stats` call stored."""

    computed_from: pd.Timestamp
    computed_to: pd.Timestamp
    hours: int
    ticks: int


class SpreadHistogram:
    """Exact per-hour-of-week counts of spreads measured in ticks."""

    def __init__(self, tick_size: float) -> None:
        self.tick_size = tick_size
        self._counts: dict[int, npt.NDArray[np.int64]] = {}

    def add(self, ts_utc: npt.NDArray[np.int64], spread: npt.NDArray[np.float64]) -> None:
        """Count ticks at UTC instants `ts_utc` with spreads `spread` (quote currency)."""
        if len(ts_utc) == 0:
            return
        in_ticks = np.rint(spread / self.tick_size).astype(np.int64)
        if (in_ticks < 0).any():
            raise ValueError("negative spreads cannot be summarised; exclude crossed quotes first")
        hours = hour_of_week(ts_utc)
        for hour in np.unique(hours):
            values = np.bincount(in_ticks[hours == hour])
            current = self._counts.get(int(hour))
            if current is None:
                self._counts[int(hour)] = values
            else:
                size = max(len(current), len(values))
                merged = np.zeros(size, dtype=np.int64)
                merged[: len(current)] += current
                merged[: len(values)] += values
                self._counts[int(hour)] = merged

    def table(self) -> pd.DataFrame:
        """``hour_of_week, p50, p90, p99, n`` for every hour that has ticks."""
        rows = []
        for hour in sorted(self._counts):
            counts = self._counts[hour]
            cumulative = np.cumsum(counts)
            total = int(cumulative[-1])
            row: dict[str, float | int] = {"hour_of_week": hour, "n": total}
            for name, q in QUANTILES.items():
                rank = int(np.ceil(q * total))
                row[name] = float(np.searchsorted(cumulative, max(rank, 1))) * self.tick_size
            rows.append(row)
        return pd.DataFrame(rows, columns=["hour_of_week", "p50", "p90", "p99", "n"])


def hour_of_week(ts_utc: npt.NDArray[np.int64]) -> npt.NDArray[np.int64]:
    """New York hour of the week (Monday 00:00 = 0) of UTC nanosecond instants."""
    local = pd.DatetimeIndex(pd.to_datetime(ts_utc, unit="ns", utc=True)).tz_convert(NEW_YORK)
    return np.asarray(local.weekday * 24 + local.hour, dtype=np.int64)


def build_spread_stats(
    cfg: AppConfig,
    engine: Engine,
    source_id: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> SpreadStatsResult:
    """Summarise spreads of the source's clean partitions into ``spread_stats`` rows.

    The window is the requested trading days (default: all clean data), cut at ``vault.start``.
    Ticks carrying any ``bars.exclude_flags`` flag are left out. Rows for the same window are
    replaced.
    """
    source = cfg.source(source_id)
    instrument = cfg.instrument(source.instrument, source.venue)
    version = rules_version(cfg.cleaning_config())
    vault_start = to_ns(pd.Timestamp(cfg.vault.start))
    mask = np.uint32(exclude_mask(cfg.bars_config()))
    histogram = SpreadHistogram(float(instrument.tick_size))

    with session_factory(engine)() as session:
        days = sorted(
            session.scalars(
                select(CleanPartition.trading_day).where(
                    CleanPartition.source_id == source_id,
                    CleanPartition.rules_version == version,
                )
            )
        )
        days = [d for d in days if (start is None or d >= start) and (end is None or d <= end)]
        days = [d for d in days if to_ns(trading_day_bounds(d)[0]) < vault_start]
        if not days:
            raise NoSpreadDataError(
                f"no clean partitions for {source_id!r} before the vault; run `xq clean` first"
            )
        count = 0
        for day in days:
            ticks = pd.read_parquet(clean_partition_path(cfg, source_id, day, version))
            ts = ticks["ts_utc"].to_numpy(dtype=np.int64)
            usable = ((ticks["flags"].to_numpy(dtype=np.uint32) & mask) == 0) & (ts < vault_start)
            spread = (ticks["ask"] - ticks["bid"]).to_numpy(dtype=np.float64)
            histogram.add(ts[usable], spread[usable])
            count += int(usable.sum())

        computed_from = trading_day_bounds(days[0])[0]
        computed_to = min(trading_day_bounds(days[-1])[1], from_ns(vault_start))
        table = histogram.table()
        session.execute(
            delete(SpreadStat).where(
                SpreadStat.source_id == source_id,
                SpreadStat.instrument_id == source.instrument,
                SpreadStat.computed_from == computed_from,
                SpreadStat.computed_to == computed_to,
            )
        )
        session.add_all(
            SpreadStat(
                source_id=source_id,
                instrument_id=source.instrument,
                hour_of_week=int(row["hour_of_week"]),
                computed_from=computed_from,
                computed_to=computed_to,
                p50=float(row["p50"]),
                p90=float(row["p90"]),
                p99=float(row["p99"]),
                n=int(row["n"]),
            )
            for row in table.to_dict("records")
        )
        session.commit()
    log.info("spread_stats_built", source_id=source_id, hours=len(table), ticks=count)
    return SpreadStatsResult(computed_from, computed_to, len(table), count)


def latest_spread_stats(engine: Engine, source_id: str) -> pd.DataFrame:
    """The most recently computed window of ``spread_stats`` for `source_id`, by hour of week."""
    with session_factory(engine)() as session:
        rows = list(session.scalars(select(SpreadStat).where(SpreadStat.source_id == source_id)))
    if not rows:
        raise NoSpreadDataError(f"no spread statistics for {source_id!r}; run `xq spread-stats`")
    latest = max((r.computed_to, r.computed_from) for r in rows)
    chosen = [r for r in rows if (r.computed_to, r.computed_from) == latest]
    return pd.DataFrame(
        [
            {"hour_of_week": r.hour_of_week, "p50": r.p50, "p90": r.p90, "p99": r.p99, "n": r.n}
            for r in chosen
        ]
    ).sort_values("hour_of_week", ignore_index=True)


class NoSpreadDataError(XQError):
    """Spread statistics were requested but there is no data to compute or read them from."""
