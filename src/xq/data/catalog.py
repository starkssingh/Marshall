"""Research-facing loaders for clean ticks and bars, with vault enforcement (DATA-010).

This is how research code reads market data. It queries the Parquet stores with DuckDB and
returns tz-aware UTC timestamps. The vault (everything at or after ``vault.start``) is enforced
here, so it cannot be bypassed by reading a different folder through the library:

- a request whose end is after ``vault.start`` raises `VaultAccessError` (it is not silently cut)
  unless it presents a one-time gate token (DS-004, `xq.datasets.vault`), which is verified
  against the metadata database, bound to the catalog's run and logged;
- without a token, ticks at or after ``vault.start`` and bars that become available after it are
  never returned, whatever the request.

Pipeline stages that must process every partition (cleaning, bar building, quality checks) read
the stores directly; they write derived data, never return it to research code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd
from sqlalchemy import Engine

from xq.core.config import AppConfig
from xq.core.errors import ConfigError
from xq.core.time import TimestampLike, ensure_utc, to_ns
from xq.core.types import PriceBasis, Timeframe
from xq.data.adapters.base import TICK_SCHEMA
from xq.data.bars import BAR_SCHEMA, BASES, OHLC, bar_set_dir, build_version
from xq.data.clean import CLEAN_DIR, clean_rules_version
from xq.datasets.vault import GateToken, check_window

TICK_TIME_COLUMNS = ("ts_utc",)
BAR_TIME_COLUMNS = ("bar_start_utc", "available_at_utc")
_NO_LIMIT = 2**63 - 1


@dataclass(frozen=True)
class Catalog:
    """Loads clean ticks and bars for research, enforcing the vault.

    `engine` and `run_id` are needed only to present a gate token: the token is verified against
    the metadata database and redeemed by (bound to) `run_id`.
    """

    cfg: AppConfig
    engine: Engine | None = None
    run_id: str | None = None

    def load_ticks(
        self,
        source: str,
        instrument: str,
        start: TimestampLike,
        end: TimestampLike,
        *,
        rules: str | None = None,
        vault_token: GateToken | None = None,
    ) -> pd.DataFrame:
        """Clean ticks with ``start <= ts_utc < end``, sorted by time.

        Args:
            source: Configured source id.
            instrument: Instrument id; must be the source's instrument.
            start, end: tz-aware bounds of the half-open interval.
            rules: Clean rules version (default: the configured rules).
            vault_token: One-time gate token (GATE-002) for a window that reaches the vault.
        """
        start_ns, end_ns, limit_ns = self._check_request(
            source, instrument, start, end, vault_token, "load_ticks"
        )
        version = rules or clean_rules_version(self.cfg)
        root = self._data_dir / CLEAN_DIR / source / instrument / f"rules={version}"
        columns = ", ".join(TICK_SCHEMA)
        query = (
            f"SELECT {columns} FROM read_parquet(?) "
            "WHERE ts_utc >= ? AND ts_utc < ? AND ts_utc < ? ORDER BY ts_utc, raw_file_id, row_num"
        )
        frame = _query(root, query, [start_ns, end_ns, limit_ns])
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
        vault_token: GateToken | None = None,
    ) -> pd.DataFrame:
        """Bars of one price basis with ``start <= bar_start_utc < end``, sorted by time.

        Without a gate token, bars that become available after ``vault.start`` are excluded.
        Price columns are renamed ``open``, ``high``, ``low``, ``close``; spread, count, flag and
        completeness columns are kept.
        """
        start_ns, end_ns, limit_ns = self._check_request(
            source, instrument, start, end, vault_token, "load_bars"
        )
        timeframe = Timeframe(tf)
        chosen = PriceBasis(basis).value
        version = build or build_version(self.cfg.bars_config(), clean_rules_version(self.cfg))
        root = bar_set_dir(self.cfg, source, timeframe, version)
        prices = [f"{chosen}_{part} AS {part}" for part in OHLC]
        others = [c for c in BAR_SCHEMA if not c.startswith(tuple(f"{b}_" for b in BASES))]
        query = (
            f"SELECT {', '.join(others[:2] + prices + others[2:])} FROM read_parquet(?) "
            "WHERE bar_start_utc >= ? AND bar_start_utc < ? AND available_at_utc <= ? "
            "ORDER BY bar_start_utc"
        )
        frame = _query(root, query, [start_ns, end_ns, limit_ns])
        schema = {**{c: BAR_SCHEMA[c] for c in others}, **dict.fromkeys(OHLC, "float64")}
        if frame is None:
            frame = pd.DataFrame({c: pd.Series(dtype=schema[c]) for c in schema})
            frame = frame[others[:2] + list(OHLC) + others[2:]]
        frame["trading_day"] = pd.to_datetime(frame["trading_day"]).dt.date
        return _with_utc(frame.astype(schema), BAR_TIME_COLUMNS)

    @property
    def _data_dir(self) -> Path:
        return self.cfg.paths.resolve(self.cfg.paths.data_dir)

    def _check_request(
        self,
        source: str,
        instrument: str,
        start: TimestampLike,
        end: TimestampLike,
        token: GateToken | None,
        purpose: str,
    ) -> tuple[int, int, int]:
        """Validate a request; return start, end and the upper bound on returned instants."""
        configured = self.cfg.source(source).instrument
        if instrument != configured:
            raise ConfigError(f"source {source!r} carries {configured!r}, not {instrument!r}")
        start_ts, end_ts = ensure_utc(start), ensure_utc(end)
        if end_ts <= start_ts:
            raise ValueError(f"end {end_ts} must be after start {start_ts}")
        granted = check_window(
            self.cfg,
            start_ts,
            end_ts,
            token=token,
            engine=self.engine,
            run_id=self.run_id,
            purpose=f"{purpose} {source}/{instrument}",
        )
        limit = _NO_LIMIT if granted else to_ns(pd.Timestamp(self.cfg.vault.start))
        return to_ns(start_ts), to_ns(end_ts), limit


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
