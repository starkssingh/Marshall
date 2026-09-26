"""MetaTrader 5 tick-export adapter (DATA-003).

MT5's "Export ticks" writes a tab-separated file with the header
``<DATE> <TIME> <BID> <ASK> <LAST> <VOLUME> <FLAGS>``, dates like ``2024.03.08``, times like
``20:59:59.123`` in the broker's server clock, and — importantly — only the fields that changed on
each row: a row that moved only the bid leaves ``<ASK>`` empty (``<FLAGS>`` 2 = bid, 4 = ask).
The quote at any row is therefore the latest bid and ask seen so far in the file.
"""

from __future__ import annotations

import codecs
import io
from pathlib import Path

import numpy as np
import pandas as pd

from xq.core.config import SourceConfig
from xq.core.errors import SourceFormatError
from xq.core.time import ClockConvention
from xq.data.adapters.base import (
    RAW_ROW_NUM,
    RAW_TS,
    TICK_SCHEMA,
    RawFileRef,
    discover_files,
)
from xq.data.flags import TickFlag
from xq.data.normalize import NormalizedTimes, canonical_order, normalize_local_times

REQUIRED_COLUMNS = ("<DATE>", "<TIME>", "<BID>", "<ASK>")
OPTIONAL_NUMERIC = {"<LAST>": "last", "<VOLUME>": "volume"}
TIME_FORMATS = ("%Y.%m.%d %H:%M:%S.%f", "%Y.%m.%d %H:%M:%S")

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


class Mt5TickAdapter:
    """Reads MT5 tick exports for one declared source."""

    def __init__(self, source_id: str, cfg: SourceConfig) -> None:
        self.source_id = source_id
        self.clock: ClockConvention = cfg.clock_convention()
        self.price_type: str = cfg.price_type
        self._patterns = list(cfg.file_patterns)
        self._encoding = cfg.encoding

    def discover(self, path: Path) -> list[RawFileRef]:
        """Return matching export files under `path`, sorted by relative path."""
        return discover_files(path, self._patterns)

    def read(self, ref: RawFileRef) -> pd.DataFrame:
        """Parse the file into a raw frame without dropping, reordering or repairing rows.

        Columns: ``row_num`` (0-based data line), ``ts_raw`` (server-clock text), ``bid``,
        ``ask``, ``last``, ``volume`` (float, NaN when the field is empty) and ``mt5_flags``.
        """
        text = _decode(ref.path.read_bytes(), self._encoding, ref.path)
        table = pd.read_csv(
            io.StringIO(text), sep="\t", dtype=str, keep_default_na=False, na_filter=False
        )
        table.columns = [str(c).strip() for c in table.columns]
        missing = [c for c in REQUIRED_COLUMNS if c not in table.columns]
        if missing:
            raise SourceFormatError(f"{ref.path}: not an MT5 tick export, missing {missing}")

        raw = pd.DataFrame(
            {
                RAW_ROW_NUM: np.arange(len(table), dtype=np.int64),
                RAW_TS: (table["<DATE>"].str.strip() + " " + table["<TIME>"].str.strip()),
                "bid": _numeric(table["<BID>"], "<BID>", ref.path),
                "ask": _numeric(table["<ASK>"], "<ASK>", ref.path),
            }
        )
        for column, name in OPTIONAL_NUMERIC.items():
            raw[name] = (
                _numeric(table[column], column, ref.path)
                if column in table.columns
                else np.full(len(table), np.nan)
            )
        flags = (
            _numeric(table["<FLAGS>"], "<FLAGS>", ref.path)
            if "<FLAGS>" in table.columns
            else pd.Series(np.nan, index=table.index)
        )
        raw["mt5_flags"] = flags.astype("Int64")
        raw[RAW_TS] = raw[RAW_TS].astype("string")
        return raw

    def timestamps(self, raw: pd.DataFrame) -> NormalizedTimes:
        """Parse ``ts_raw`` and convert it to UTC with the declared clock."""
        return normalize_local_times(_parse_local(raw[RAW_TS]), self.clock)

    def to_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Carry each side's latest value forward in file order, then sort stably by UTC time.

        Rows before both sides have been seen keep NaN and are flagged `MISSING_QUOTE`.
        """
        times = self.timestamps(raw)
        bid = raw["bid"].ffill().to_numpy(dtype=np.float64)
        ask = raw["ask"].ffill().to_numpy(dtype=np.float64)
        flags = times.flags.copy()
        flags[np.isnan(bid) | np.isnan(ask)] |= np.uint32(TickFlag.MISSING_QUOTE)
        raw_file_id = (
            raw["raw_file_id"].astype("string")
            if "raw_file_id" in raw.columns
            else pd.Series(pd.NA, index=raw.index, dtype="string")
        )
        ticks = pd.DataFrame(
            {
                "ts_utc": times.ts_utc,
                "bid": bid,
                "ask": ask,
                "bid_size": np.full(len(raw), np.nan),
                "ask_size": np.full(len(raw), np.nan),
                "flags": flags,
                "raw_file_id": raw_file_id.to_numpy(),
                "row_num": raw[RAW_ROW_NUM].to_numpy(dtype=np.int64),
            }
        )
        ticks = ticks.iloc[canonical_order(times.ts_utc)].reset_index(drop=True)
        return ticks.astype(TICK_SCHEMA)


def _decode(data: bytes, encoding: str | None, path: Path) -> str:
    if encoding is None:
        encoding = next((name for bom, name in _BOMS if data.startswith(bom)), "utf-8")
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise SourceFormatError(f"{path}: cannot decode as {encoding}: {exc}") from exc


def _numeric(values: pd.Series, column: str, path: Path) -> pd.Series:
    stripped = values.str.strip()
    empty = stripped == ""
    parsed = pd.to_numeric(stripped.where(~empty), errors="coerce")
    bad = parsed.isna() & ~empty
    if bad.any():
        row = int(np.flatnonzero(bad.to_numpy())[0])
        raise SourceFormatError(f"{path}: {column} value {values.iloc[row]!r} at data row {row}")
    return parsed.astype(np.float64)


def _parse_local(ts_raw: pd.Series) -> pd.DatetimeIndex:
    """Parse each row with the first format that fits it, so a file may mix both formats."""
    parsed = pd.Series(pd.NaT, index=ts_raw.index, dtype="datetime64[ns]")
    for fmt in TIME_FORMATS:
        missing = parsed.isna()
        if not missing.any():
            break
        parsed[missing] = pd.to_datetime(ts_raw[missing], format=fmt, errors="coerce")
    unparsed = np.flatnonzero(parsed.isna().to_numpy())
    if len(unparsed):
        row = int(unparsed[0])
        raise SourceFormatError(f"unparseable timestamp {ts_raw.iloc[row]!r} at data row {row}")
    return pd.DatetimeIndex(parsed).as_unit("ns")
