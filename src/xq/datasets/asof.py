"""Point-in-time joins on availability (DS-002).

`asof_join` attaches to each row of `left` (a decision time) the latest row of `right` that was
*available* at that time: ``right.available_at <= left.decision_time``. Joining higher-timeframe
bars on their start instead would hand a decision the whole bar before it closed; that join is
refused here by requiring the right key to be an availability column.

The matched row's ``available_at`` is kept as a provenance column (``<prefix>available_at``), so
the leakage harness (DS-006) can audit every joined value.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import timedelta

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.errors import NaiveTimestampError

_AVAILABILITY = re.compile(r"available_at")
_NAT = np.iinfo(np.int64).min  # how NaT is stored in int64 nanoseconds
PROVENANCE_SUFFIX = "available_at"


def asof_join(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    on_left: str = "decision_time",
    on_right: str = "available_at",
    tolerance: pd.Timedelta | timedelta | str | None = None,
    prefix: str = "",
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Backward as-of join of `right` onto `left` on availability.

    Args:
        left: Rows to enrich; `on_left` holds tz-aware decision times. Order and index are kept.
        right: Rows with a tz-aware availability column `on_right`. When several rows share an
            availability time, the last one in `right`'s order wins.
        on_left: Decision-time column of `left`.
        on_right: Availability column of `right`; its name must contain ``available_at``.
        tolerance: If given, a match older than this is dropped (values become missing).
        prefix: Prepended to every joined column name, e.g. ``"h1_"``.
        columns: Columns of `right` to attach (default: all except `on_right`).

    Returns:
        `left` with the chosen `right` columns and ``<prefix>available_at`` (the matched row's
        availability, NaT when there is no match).

    Raises:
        ValueError: if `on_right` is not an availability column, a joined name collides with a
            `left` column, or `right` has missing availability times.
        NaiveTimestampError: if either key is not a tz-aware datetime column.
    """
    if not _AVAILABILITY.search(on_right):
        raise ValueError(
            f"asof_join joins on availability; {on_right!r} is not an available_at column "
            "(joining on bar start leaks the rest of the bar)"
        )
    left_keys = _utc_ns(left[on_left], on_left)
    right_keys = _utc_ns(right[on_right], on_right)
    if right[on_right].isna().any():
        raise ValueError(f"right column {on_right!r} has missing availability times")

    chosen = [c for c in (columns if columns is not None else right.columns) if c != on_right]
    provenance = f"{prefix}{PROVENANCE_SUFFIX}"
    joined_names = [f"{prefix}{c}" for c in chosen] + [provenance]
    clashes = sorted(set(joined_names) & set(left.columns))
    if clashes:
        raise ValueError(f"joined columns would overwrite left columns: {clashes}")

    order = np.argsort(right_keys, kind="stable")
    sorted_keys = right_keys[order]
    # Index of the last right row with available_at <= decision time (-1 if none).
    position = np.searchsorted(sorted_keys, left_keys, side="right") - 1
    valid = (position >= 0) & (left_keys != _NAT)
    rows = np.full(len(left_keys), -1, dtype=np.int64)
    matched = np.full(len(left_keys), _NAT, dtype=np.int64)
    matched[valid] = sorted_keys[position[valid]]
    if tolerance is not None:
        stale = np.zeros(len(left_keys), dtype=bool)
        stale[valid] = left_keys[valid] - matched[valid] > pd.Timedelta(tolerance).value
        valid &= ~stale
        matched[stale] = _NAT
    rows[valid] = order[position[valid]]

    result = left.copy()
    for name, column in zip(joined_names[:-1], chosen, strict=True):
        # -1 is not a label of the reset index, so unmatched rows become missing (ints -> float).
        taken = right[column].reset_index(drop=True).reindex(rows)
        result[name] = pd.Series(taken.array, index=left.index)
    result[provenance] = pd.Series(pd.to_datetime(matched, unit="ns", utc=True), index=left.index)
    return result


def _utc_ns(column: pd.Series, name: str) -> npt.NDArray[np.int64]:
    dtype = column.dtype
    if not isinstance(dtype, pd.DatetimeTZDtype):
        raise NaiveTimestampError(
            f"column {name!r} must hold tz-aware timestamps, got dtype {dtype}"
        )
    values: npt.NDArray[np.int64] = (
        column.dt.tz_convert("UTC").dt.as_unit("ns").to_numpy(dtype="datetime64[ns]").view(np.int64)
    )
    return values
