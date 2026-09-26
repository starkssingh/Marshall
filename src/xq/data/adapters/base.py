"""The `SourceAdapter` protocol and the canonical tick frame (DATA-003).

An adapter knows one file format and one source's declared conventions. It

- `discover`s the files under a path,
- `read`s a file into a *raw frame* — every data line, in file order, with its original timestamp
  text (``ts_raw``) and typed values; nothing is dropped, reordered or repaired,
- converts raw timestamps to UTC with the declared clock (`timestamps`), and
- builds canonical ticks (`to_canonical`).

Canonical ticks (``TickFrame``) have exactly the columns in `TICK_SCHEMA`, sorted stably by
``ts_utc``, with provenance (``raw_file_id``, ``row_num``) back to the raw file.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd

from xq.core.errors import SourceFormatError
from xq.core.time import ClockConvention
from xq.data.normalize import NormalizedTimes

#: Canonical tick columns and dtypes. Prices are in quote currency; sizes are NaN when unknown.
TICK_SCHEMA: dict[str, str] = {
    "ts_utc": "int64",
    "bid": "float64",
    "ask": "float64",
    "bid_size": "float64",
    "ask_size": "float64",
    "flags": "uint32",
    "raw_file_id": "string",
    "row_num": "int64",
}

#: Columns every raw frame starts with.
RAW_ROW_NUM = "row_num"
RAW_TS = "ts_raw"


@dataclass(frozen=True)
class RawFileRef:
    """A file found by `SourceAdapter.discover`."""

    path: Path
    original_name: str
    size: int


class SourceAdapter(Protocol):
    """Reads one source's files. Implementations declare the source's conventions."""

    source_id: str
    clock: ClockConvention
    price_type: str

    def discover(self, path: Path) -> list[RawFileRef]:
        """Return the source files at `path` (a file or a directory), in a stable order."""
        ...

    def read(self, ref: RawFileRef) -> pd.DataFrame:
        """Return the raw frame: one row per data line, in file order, values unmodified."""
        ...

    def timestamps(self, raw: pd.DataFrame) -> NormalizedTimes:
        """Convert the raw frame's timestamps to UTC with the declared clock."""
        ...

    def to_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Return canonical ticks (see `TICK_SCHEMA`) built from a raw frame."""
        ...


def discover_files(path: Path, patterns: list[str]) -> list[RawFileRef]:
    """Files at `path` matching any of `patterns`, recursively, sorted by relative path.

    Hidden files and directories (names starting with ``.``) are skipped.
    """
    if path.is_file():
        candidates = [path]
        base = path.parent
    elif path.is_dir():
        candidates = [p for p in path.rglob("*") if p.is_file()]
        base = path
    else:
        raise SourceFormatError(f"no such file or directory: {path}")

    refs = []
    for candidate in candidates:
        relative = candidate.relative_to(base)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if any(fnmatch.fnmatch(candidate.name, pattern) for pattern in patterns):
            refs.append((relative.as_posix(), candidate))
    return [
        RawFileRef(path=candidate, original_name=candidate.name, size=candidate.stat().st_size)
        for _, candidate in sorted(refs)
    ]


def validate_tick_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Check that `frame` has exactly the canonical columns, dtypes and ordering."""
    columns = list(frame.columns)
    if columns != list(TICK_SCHEMA):
        raise SourceFormatError(f"tick frame columns {columns} != {list(TICK_SCHEMA)}")
    for column, dtype in TICK_SCHEMA.items():
        if str(frame[column].dtype) != dtype:
            raise SourceFormatError(f"tick column {column!r} has dtype {frame[column].dtype}")
    if not frame["ts_utc"].is_monotonic_increasing:
        raise SourceFormatError("tick frame is not sorted by ts_utc")
    return frame
