"""Dukascopy tick adapter (DATA-013, ADR 0057).

Dukascopy quotes are in UTC. The adapter reads two file formats of the same data:

1. **Native hourly files** (``.bi5``), exactly as `xq fetch dukascopy` stores the vendor's bytes,
   named ``<SYMBOL>_<YYYY-MM-DD>_<HH>h_ticks.bi5`` after the UTC hour they cover. The name carries
   the hour because the bytes do not, and the raw store keeps a file's name but not its folder.
   A file is an LZMA stream ("alone" format) of 20-byte big-endian records ``>i4 >i4 >i4 >f4 >f4``:
   milliseconds since the start of the hour, ask and bid in integer points, ask and bid volume.
   A price is its points divided by the source's `point_scale` (1000 for XAUUSD). An empty file is
   an hour without ticks.
2. **CSV** as exported by dukascopy-node (``-t tick -f csv``), the fallback while the ``.bi5``
   endpoint is unavailable (ADR 0057): header ``timestamp,askPrice,bidPrice`` optionally followed
   by ``askVolume,bidVolume``; timestamps in Unix milliseconds or ISO 8601 text (UTC).

Timestamps go through the declared clock like every other source (`normalize_local_times`), so a
misdeclared clock shows up in the gap checks instead of being assumed away. Every row carries both
sides of the quote, so there is no carry-forward; a missing side is flagged `MISSING_QUOTE`.
Volumes stay in the raw frame and the Parquet mirror, but canonical sizes are NaN: the unit of
Dukascopy's gold volumes is not documented, and tick volume is a feed artifact (FEAT-007 gated).
"""

from __future__ import annotations

import io
import lzma
import re
from datetime import date
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import SourceConfig
from xq.core.errors import ConfigError, SourceFormatError
from xq.core.time import ClockConvention, ensure_utc
from xq.data.adapters.base import RAW_ROW_NUM, RAW_TS, TICK_SCHEMA, RawFileRef, discover_files
from xq.data.flags import TickFlag
from xq.data.normalize import NormalizedTimes, canonical_order, normalize_local_times

#: One tick record of a ``.bi5`` file after decompression (big-endian).
BI5_RECORD: Final = np.dtype(
    [("ms", ">i4"), ("ask", ">i4"), ("bid", ">i4"), ("ask_volume", ">f4"), ("bid_volume", ">f4")]
)
HOUR_MS: Final = 3_600_000
#: Typed column holding each row's time in the source clock, as milliseconds since 1970-01-01.
TS_SOURCE_MS: Final = "ts_source_ms"
CSV_COLUMNS: Final = ("timestamp", "askPrice", "bidPrice")
CSV_VOLUMES: Final = ("askVolume", "bidVolume")

_BI5_NAME = re.compile(
    r"^(?P<symbol>[A-Z0-9]+)_(?P<day>\d{4}-\d{2}-\d{2})_(?P<hour>\d{2})h_ticks\.bi5$"
)
_EPOCH_MS = re.compile(r"^\d+$")
_UTC_SUFFIX = re.compile(r"(Z|[+-]00:?00)$", re.IGNORECASE)
_OTHER_OFFSET = re.compile(r"[+-]\d{2}:?\d{2}$")
_HOUR = pd.Timedelta(hours=1)


def bi5_file_name(symbol: str, hour_start: pd.Timestamp) -> str:
    """Name of the native file of `symbol` for the UTC hour starting at `hour_start`."""
    hour = _whole_utc_hour(hour_start)
    return f"{symbol}_{hour:%Y-%m-%d}_{hour:%H}h_ticks.bi5"


def parse_bi5_name(name: str) -> tuple[str, pd.Timestamp]:
    """Return the symbol and UTC hour start encoded in a native file's name."""
    match = _BI5_NAME.match(name)
    if match is None:
        raise SourceFormatError(
            f"{name!r} is not a Dukascopy hourly file name (<SYMBOL>_<YYYY-MM-DD>_<HH>h_ticks.bi5)"
        )
    try:
        day = date.fromisoformat(match["day"])
    except ValueError as exc:
        raise SourceFormatError(f"{name!r} has an invalid date: {exc}") from exc
    hour = int(match["hour"])
    if hour > 23:
        raise SourceFormatError(f"{name!r} has an invalid hour {hour}")
    start = pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hour, tz="UTC")
    return match["symbol"], start


def datafeed_url(base_url: str, symbol: str, hour_start: pd.Timestamp) -> str:
    """URL of the vendor's hourly tick file. Dukascopy numbers months from 00 (January)."""
    hour = _whole_utc_hour(hour_start)
    return (
        f"{base_url.rstrip('/')}/{symbol}/{hour.year:04d}/{hour.month - 1:02d}/"
        f"{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
    )


def decode_bi5(data: bytes) -> npt.NDArray[np.void]:
    """Decompress a ``.bi5`` payload into `BI5_RECORD` rows (none for an empty payload).

    Raises `SourceFormatError` unless the payload is an LZMA stream of whole records whose
    offsets all lie within the hour.
    """
    if not data:
        return np.zeros(0, dtype=BI5_RECORD)
    try:
        payload = lzma.decompress(data, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError as exc:
        raise SourceFormatError(f"not an LZMA stream: {exc}") from exc
    if len(payload) % BI5_RECORD.itemsize:
        raise SourceFormatError(
            f"{len(payload)} decompressed bytes are not whole {BI5_RECORD.itemsize}-byte records"
        )
    records: npt.NDArray[np.void] = np.frombuffer(payload, dtype=BI5_RECORD)
    ms = records["ms"].astype(np.int64)
    outside = np.flatnonzero((ms < 0) | (ms >= HOUR_MS))
    if len(outside):
        row = int(outside[0])
        raise SourceFormatError(
            f"record {row} is {ms[row]} ms from the hour start, outside [0, {HOUR_MS})"
        )
    return records


class DukascopyTickAdapter:
    """Reads Dukascopy tick files (native hourly ``.bi5`` or dukascopy-node CSV) for one source."""

    def __init__(self, source_id: str, cfg: SourceConfig) -> None:
        if cfg.vendor_symbol is None or cfg.point_scale is None:  # pragma: no cover - validated
            raise ConfigError(f"source {source_id!r} needs vendor_symbol and point_scale")
        self.source_id = source_id
        self.clock: ClockConvention = cfg.clock_convention()
        self.price_type: str = cfg.price_type
        self.vendor_symbol: str = cfg.vendor_symbol
        self.point_scale: int = cfg.point_scale
        self._patterns = list(cfg.file_patterns)

    def discover(self, path: Path) -> list[RawFileRef]:
        """Return matching files under `path`, sorted by relative path."""
        return discover_files(path, self._patterns)

    def read(self, ref: RawFileRef) -> pd.DataFrame:
        """Parse the file into a raw frame without dropping, reordering or repairing rows.

        Columns: ``row_num``, ``ts_raw`` (the file's own time field as text: the millisecond
        offset of a ``.bi5`` record, or the CSV cell), ``ts_source_ms`` (the row's time in the
        source clock, ms since 1970-01-01), then the format's typed values ending with ``bid``,
        ``ask``, ``bid_volume`` and ``ask_volume``. A ``.bi5`` frame also keeps the integer points
        (``ask_points``, ``bid_points``) it scaled the prices from.
        """
        suffix = Path(ref.original_name).suffix.lower()
        if suffix == ".bi5":
            return self._read_bi5(ref)
        if suffix == ".csv":
            return self._read_csv(ref)
        raise SourceFormatError(f"{ref.original_name}: expected a .bi5 or .csv file")

    def timestamps(self, raw: pd.DataFrame) -> NormalizedTimes:
        """Convert ``ts_source_ms`` to UTC with the declared clock."""
        millis = raw[TS_SOURCE_MS].to_numpy(dtype=np.int64)
        local = pd.DatetimeIndex(pd.to_datetime(millis, unit="ms")).as_unit("ns")
        return normalize_local_times(local, self.clock)

    def to_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Canonical ticks sorted stably by UTC time; a row missing a side is `MISSING_QUOTE`."""
        times = self.timestamps(raw)
        bid = raw["bid"].to_numpy(dtype=np.float64)
        ask = raw["ask"].to_numpy(dtype=np.float64)
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

    def _read_bi5(self, ref: RawFileRef) -> pd.DataFrame:
        symbol, hour_start = parse_bi5_name(ref.original_name)
        if symbol != self.vendor_symbol:
            raise SourceFormatError(
                f"{ref.original_name}: symbol {symbol} is not the source's {self.vendor_symbol}"
            )
        try:
            records = decode_bi5(ref.path.read_bytes())
        except SourceFormatError as exc:
            raise SourceFormatError(f"{ref.original_name}: {exc}") from exc
        ms = records["ms"].astype(np.int64)
        ask_points = records["ask"].astype(np.int64)
        bid_points = records["bid"].astype(np.int64)
        hour_ms = int(hour_start.value // 1_000_000)
        raw = pd.DataFrame(
            {
                RAW_ROW_NUM: np.arange(len(records), dtype=np.int64),
                RAW_TS: pd.array(ms.astype(str), dtype="string"),
                TS_SOURCE_MS: hour_ms + ms,
                "ask_points": ask_points,
                "bid_points": bid_points,
                "bid": bid_points / self.point_scale,
                "ask": ask_points / self.point_scale,
                "bid_volume": records["bid_volume"].astype(np.float64),
                "ask_volume": records["ask_volume"].astype(np.float64),
            }
        )
        return raw

    def _read_csv(self, ref: RawFileRef) -> pd.DataFrame:
        text = ref.path.read_bytes().decode("utf-8-sig")
        if not text.strip():
            table = pd.DataFrame({c: pd.Series(dtype=str) for c in CSV_COLUMNS})
        else:
            table = pd.read_csv(
                io.StringIO(text), dtype=str, keep_default_na=False, na_filter=False
            )
            table.columns = [str(c).strip() for c in table.columns]
        missing = [c for c in CSV_COLUMNS if c not in table.columns]
        if missing:
            raise SourceFormatError(
                f"{ref.original_name}: not a dukascopy-node tick CSV, missing {missing}"
            )
        volumes = {
            name: (
                _numeric(table[column], column, ref.original_name)
                if column in table.columns
                else pd.Series(np.nan, index=table.index)
            )
            for name, column in zip(("ask_volume", "bid_volume"), CSV_VOLUMES, strict=True)
        }
        cells = table["timestamp"].str.strip()
        raw = pd.DataFrame(
            {
                RAW_ROW_NUM: np.arange(len(table), dtype=np.int64),
                RAW_TS: cells.astype("string"),
                TS_SOURCE_MS: _source_millis(cells, ref.original_name),
                "bid": _numeric(table["bidPrice"], "bidPrice", ref.original_name),
                "ask": _numeric(table["askPrice"], "askPrice", ref.original_name),
                "bid_volume": volumes["bid_volume"],
                "ask_volume": volumes["ask_volume"],
            }
        )
        return raw


def _whole_utc_hour(ts: pd.Timestamp) -> pd.Timestamp:
    hour = ensure_utc(ts)
    if hour != hour.floor("h"):
        raise ValueError(f"{hour} is not the start of a UTC hour")
    return hour


def _numeric(values: pd.Series, column: str, name: str) -> pd.Series:
    stripped = values.str.strip()
    empty = stripped == ""
    parsed = pd.to_numeric(stripped.where(~empty), errors="coerce")
    bad = parsed.isna() & ~empty
    if bad.any():
        row = int(np.flatnonzero(bad.to_numpy())[0])
        raise SourceFormatError(f"{name}: {column} value {values.iloc[row]!r} at data row {row}")
    return parsed.astype(np.float64)


def _source_millis(cells: pd.Series, name: str) -> npt.NDArray[np.int64]:
    """Milliseconds since 1970-01-01 in the source clock, from Unix-ms or ISO 8601 cells.

    An ISO cell may be naive (read in the source clock) or carry a zero UTC offset; any other
    offset contradicts the source's declared UTC clock and is refused.
    """
    result = np.zeros(len(cells), dtype=np.int64)
    epoch = cells.str.fullmatch(_EPOCH_MS.pattern).to_numpy(dtype=bool)
    if epoch.any():
        result[epoch] = cells[epoch].astype(np.int64).to_numpy()
    text = cells[~epoch]
    if len(text):
        stripped = text.str.replace(_UTC_SUFFIX, "", regex=True)
        offset = stripped.str.contains(_OTHER_OFFSET.pattern, regex=True).to_numpy(dtype=bool)
        if offset.any():
            row = int(np.flatnonzero(~epoch)[np.flatnonzero(offset)[0]])
            raise SourceFormatError(
                f"{name}: timestamp {cells.iloc[row]!r} at data row {row} is not UTC"
            )
        try:
            parsed = pd.to_datetime(stripped, format="ISO8601", errors="coerce")
        except (TypeError, ValueError) as exc:  # e.g. a mix of offsets
            raise SourceFormatError(f"{name}: timestamps are not UTC ISO 8601: {exc}") from exc
        if parsed.dt.tz is not None:
            raise SourceFormatError(f"{name}: timestamps carry a UTC offset other than zero")
        bad = np.flatnonzero(parsed.isna().to_numpy())
        if len(bad):
            row = int(np.flatnonzero(~epoch)[bad[0]])
            raise SourceFormatError(
                f"{name}: unparseable timestamp {cells.iloc[row]!r} at data row {row}"
            )
        nanos = (
            pd.DatetimeIndex(parsed).as_unit("ns").to_numpy(dtype="datetime64[ns]").view(np.int64)
        )
        result[~epoch] = nanos // 1_000_000
    return result
