"""Research-facing loaders for clean ticks and bars, with vault enforcement (DATA-010).

This is how research code reads market data. It queries the Parquet stores with DuckDB and
returns tz-aware UTC timestamps. The vault (everything at or after ``vault.start``) is enforced
here, so it cannot be bypassed by reading a different folder through the library:

- a request whose end is after ``vault.start`` raises `VaultAccessError` (it is not silently cut);
- ticks at or after ``vault.start`` and bars that become available after it are never returned,
  whatever the request;
- ``allow_vault=True`` raises too, until the gate-token flow (DS-004, GATE-002) exists.

Pipeline stages that must process every partition (cleaning, bar building, quality checks) read
the stores directly; they write derived data, never return it to research code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from xq.core.config import AppConfig
from xq.core.errors import ConfigError, VaultAccessError
from xq.core.time import TimestampLike, ensure_utc, to_ns
from xq.core.types import PriceBasis, Timeframe
from xq.data.adapters.base import TICK_SCHEMA
from xq.data.bars import BAR_SCHEMA, BASES, OHLC, bar_set_dir, build_version
from xq.data.clean import CLEAN_DIR, rules_version

TICK_TIME_COLUMNS = ("ts_utc",)
BAR_TIME_COLUMNS = ("bar_start_utc", "available_at_utc")


@dataclass(frozen=True)
class Catalog:
    """Loads clean ticks and bars for research, enforcing the vault."""

    cfg: AppConfig

    def load_ticks(
        self,
        source: str,
        instrument: str,
        start: TimestampLike,
        end: TimestampLike,
        *,
        rules: str | None = None,
        allow_vault: bool = False,
    ) -> pd.DataFrame:
        """Clean ticks with ``start <= ts_utc < end``, sorted by time.

        Args:
            source: Configured source id.
            instrument: Instrument id; must be the source's instrument.
            start, end: tz-aware bounds of the half-open interval.
            rules: Clean rules version (default: the configured rules).
            allow_vault: Reserved for gate-token access (DS-004); True raises for now.
        """
        start_ns, end_ns = self._check_request(source, instrument, start, end, allow_vault)
        version = rules or rules_version(self.cfg.cleaning_config())
        root = self._data_dir / CLEAN_DIR / source / instrument / f"rules={version}"
        columns = ", ".join(TICK_SCHEMA)
        query = (
            f"SELECT {columns} FROM read_parquet(?) "
            "WHERE ts_utc >= ? AND ts_utc < ? AND ts_utc < ? ORDER BY ts_utc, raw_file_id, row_num"
        )
        frame = _query(root, query, [start_ns, end_ns, self._vault_ns])
        if frame is None:
            frame = pd.DataFrame({c: pd.Series(dtype=d) for c, d in TICK_SCHEMA.items()})
        return _with_utc(frame.astype(TICK_SCHEMA), TICK_TIME_COLUMNS)

    def load_bars(
        self,
        source: str,
        instrument: str,
        tf: Timeframe | str,
        basis: PriceBasis | str,
        start: TimestampLike,
        end: TimestampLike,
        *,
        build: str | None = None,
        allow_vault: bool = False,
    ) -> pd.DataFrame:
        """Bars of one price basis with ``start <= bar_start_utc < end``, sorted by time.

        Bars that become available after ``vault.start`` are excluded. Price columns are renamed
        ``open``, ``high``, ``low``, ``close``; spread, count, flag and completeness columns are
        kept.
        """
        start_ns, end_ns = self._check_request(source, instrument, start, end, allow_vault)
        timeframe = Timeframe(tf)
        chosen = PriceBasis(basis).value
        version = build or build_version(
            self.cfg.bars_config(), rules_version(self.cfg.cleaning_config())
        )
        root = bar_set_dir(self.cfg, source, timeframe, version)
        prices = [f"{chosen}_{part} AS {part}" for part in OHLC]
        others = [c for c in BAR_SCHEMA if not c.startswith(tuple(f"{b}_" for b in BASES))]
        query = (
            f"SELECT {', '.join(others[:2] + prices + others[2:])} FROM read_parquet(?) "
            "WHERE bar_start_utc >= ? AND bar_start_utc < ? AND available_at_utc <= ? "
            "ORDER BY bar_start_utc"
        )
        frame = _query(root, query, [start_ns, end_ns, self._vault_ns])
        schema = {**{c: BAR_SCHEMA[c] for c in others}, **dict.fromkeys(OHLC, "float64")}
        if frame is None:
            frame = pd.DataFrame({c: pd.Series(dtype=schema[c]) for c in schema})
            frame = frame[others[:2] + list(OHLC) + others[2:]]
        frame["trading_day"] = pd.to_datetime(frame["trading_day"]).dt.date
        return _with_utc(frame.astype(schema), BAR_TIME_COLUMNS)

    @property
    def _data_dir(self) -> Path:
        return self.cfg.paths.resolve(self.cfg.paths.data_dir)

    @property
    def _vault_ns(self) -> int:
        return to_ns(pd.Timestamp(self.cfg.vault.start))

    def _check_request(
        self,
        source: str,
        instrument: str,
        start: TimestampLike,
        end: TimestampLike,
        allow_vault: bool,
    ) -> tuple[int, int]:
        configured = self.cfg.source(source).instrument
        if instrument != configured:
            raise ConfigError(f"source {source!r} carries {configured!r}, not {instrument!r}")
        if allow_vault:
            raise VaultAccessError(
                "vault access needs a one-time gate token (DS-004, GATE-002), which does not "
                "exist yet"
            )
        start_ts, end_ts = ensure_utc(start), ensure_utc(end)
        if end_ts <= start_ts:
            raise ValueError(f"end {end_ts} must be after start {start_ts}")
        vault = pd.Timestamp(self.cfg.vault.start)
        if end_ts > vault:
            raise VaultAccessError(
                f"requested data up to {end_ts}, but the vault starts at {vault}; "
                "research data must end at or before vault.start"
            )
        return to_ns(start_ts), to_ns(end_ts)


def _query(root: Path, query: str, parameters: list[int]) -> pd.DataFrame | None:
    files = sorted(str(p) for p in root.rglob("*.parquet"))
    if not files:
        return None
    with duckdb.connect() as connection:
        result: pd.DataFrame = connection.execute(query, [files, *parameters]).df()
    return result


def _with_utc(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    for column in columns:
        frame[column] = pd.to_datetime(frame[column], unit="ns", utc=True)
    return frame.reset_index(drop=True)
