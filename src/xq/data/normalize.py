"""Conversion of source-clock timestamps to UTC nanoseconds (DATA-006).

Every adapter declares a `ClockConvention`; nothing assumes broker server time is UTC. Around DST
changes some local times happen twice or not at all. Those rows are converted deterministically and
flagged, never dropped:

- ambiguous (repeated) wall times are read as the first occurrence until the file's clock jumps
  backwards inside the repeated hour, and as the second occurrence after it;
- nonexistent wall times are shifted forward to the transition instant.

Within a file, rows that are earlier than a preceding row after conversion are flagged
`TS_OUT_OF_ORDER`; `canonical_order` then gives a stable sort by UTC time.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.errors import ClockConventionError
from xq.core.time import ClockConvention, ClockKind
from xq.data.flags import FLAG_DTYPE, TickFlag

_HOUR_NS = 3_600 * 1_000_000_000


@dataclass(frozen=True)
class NormalizedTimes:
    """UTC nanoseconds and per-row flags, aligned with the input rows."""

    ts_utc: npt.NDArray[np.int64]
    flags: npt.NDArray[np.uint32]


def normalize_local_times(local: pd.DatetimeIndex, clock: ClockConvention) -> NormalizedTimes:
    """Convert naive source-clock timestamps (in file order) to UTC nanoseconds.

    Args:
        local: Naive timestamps exactly as written by the source, in file order.
        clock: The source's declared clock convention.
    """
    if local.tz is not None:
        raise ClockConventionError("source-clock timestamps must be naive; the clock gives meaning")
    wall = local.as_unit("ns")
    flags = np.zeros(len(wall), dtype=FLAG_DTYPE)

    if clock.kind is ClockKind.UTC:
        utc = wall.tz_localize("UTC")
    elif clock.kind is ClockKind.FIXED:
        utc = (wall - pd.Timedelta(minutes=clock.offset_minutes)).tz_localize("UTC")
    else:
        zone = clock.tz
        if zone is None:  # pragma: no cover - guaranteed by ClockConvention.parse
            raise ValueError(f"clock {clock} has no time zone")
        zone_wall = wall - pd.Timedelta(hours=clock.shift_hours)
        utc, zone_flags = _localize_zone_wall_time(zone_wall, zone)
        flags |= zone_flags

    ts_utc = _nanos(utc.tz_convert("UTC").tz_localize(None))
    flags[out_of_order(ts_utc)] |= np.uint32(TickFlag.TS_OUT_OF_ORDER)
    return NormalizedTimes(ts_utc=ts_utc, flags=flags)


def out_of_order(ts_utc: npt.NDArray[np.int64]) -> npt.NDArray[np.bool_]:
    """Rows earlier than some preceding row (equal timestamps are in order)."""
    if len(ts_utc) == 0:
        return np.zeros(0, dtype=bool)
    running_max = np.maximum.accumulate(ts_utc)
    previous_max = np.concatenate(([np.iinfo(np.int64).min], running_max[:-1]))
    result: npt.NDArray[np.bool_] = ts_utc < previous_max
    return result


def canonical_order(ts_utc: npt.NDArray[np.int64]) -> npt.NDArray[np.intp]:
    """Stable ordering by UTC time: ties keep their file order."""
    order: npt.NDArray[np.intp] = np.argsort(ts_utc, kind="stable")
    return order


def _localize_zone_wall_time(
    wall: pd.DatetimeIndex, zone: str
) -> tuple[pd.DatetimeIndex, npt.NDArray[np.uint32]]:
    flags = np.zeros(len(wall), dtype=FLAG_DTYPE)
    as_dst = np.ones(len(wall), dtype=bool)
    nonexistent = np.asarray(
        wall.tz_localize(zone, ambiguous=as_dst, nonexistent="NaT").isna(), dtype=bool
    )
    unresolved = np.asarray(wall.tz_localize(zone, ambiguous="NaT", nonexistent="NaT").isna())
    ambiguous = unresolved & ~nonexistent

    is_dst = _resolve_ambiguous(wall, ambiguous)
    utc = wall.tz_localize(zone, ambiguous=is_dst, nonexistent="shift_forward")
    flags[ambiguous] |= np.uint32(TickFlag.TS_DST_AMBIGUOUS)
    flags[nonexistent] |= np.uint32(TickFlag.TS_DST_NONEXISTENT)
    return utc, flags


def _resolve_ambiguous(
    wall: pd.DatetimeIndex, ambiguous: npt.NDArray[np.bool_]
) -> npt.NDArray[np.bool_]:
    """DST (first occurrence) until the clock jumps backwards within a run of ambiguous rows."""
    is_dst = np.ones(len(wall), dtype=bool)
    if not ambiguous.any():
        return is_dst
    values = _nanos(wall)
    positions = np.flatnonzero(ambiguous)
    # A run is consecutive ambiguous rows of one DST transition: adjacent in the file and less
    # than an hour apart in wall time (the repeated interval is one hour long).
    not_adjacent = np.diff(positions) != 1
    other_transition = np.abs(np.diff(values[positions])) >= _HOUR_NS
    run_breaks = np.flatnonzero(not_adjacent | other_transition) + 1
    for run in np.split(positions, run_breaks):
        jumps = np.flatnonzero(np.diff(values[run]) < 0)
        if len(jumps):
            is_dst[run[jumps[0] + 1 :]] = False
    return is_dst


def _nanos(naive: pd.DatetimeIndex) -> npt.NDArray[np.int64]:
    values: npt.NDArray[np.int64] = naive.to_numpy(dtype="datetime64[ns]").view(np.int64)
    return values
