"""Metadata database models (DATA-005).

The metadata database records provenance: where every file came from, which run ingested it,
and — from Sprint 2 — how clean ticks, bars and spread statistics were derived. It is SQLite in the
research tier and moves to PostgreSQL with paper trading (PAPER-004); the models use only portable
types.

Every instant is stored as UTC int64 nanoseconds (convention 1) through `NanoTimestamp`, which
accepts and returns tz-aware pandas timestamps and rejects naive ones. Exact decimals (contract
terms) are stored as text through `DecimalText`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    Dialect,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from xq.core.time import from_ns, to_ns

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class NanoTimestamp(TypeDecorator[pd.Timestamp]):
    """UTC instant stored as int64 nanoseconds since the epoch."""

    impl = BigInteger
    cache_ok = True

    def process_bind_param(
        self, value: pd.Timestamp | datetime | None, dialect: Dialect
    ) -> int | None:
        return None if value is None else to_ns(value)

    def process_result_value(self, value: int | None, dialect: Dialect) -> pd.Timestamp | None:
        return None if value is None else from_ns(value)


class DecimalText(TypeDecorator[Decimal]):
    """Exact decimal stored as its canonical text (SQLite has no native decimal type)."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    """Declarative base with a naming convention so migrations get stable constraint names."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        pd.Timestamp: NanoTimestamp(),
        Decimal: DecimalText(),
        dict[str, Any]: JSON(),
        list[str]: JSON(),
    }


class DataSource(Base):
    """A declared data feed. Its clock convention is fixed for the life of the source id."""

    __tablename__ = "data_sources"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    vendor: Mapped[str] = mapped_column(String(128))
    feed_type: Mapped[str] = mapped_column(String(32))
    venue: Mapped[str] = mapped_column(String(64))
    price_type: Mapped[str] = mapped_column(String(32))
    clock_convention: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str] = mapped_column(Text, default="")


class Instrument(Base):
    """Contract terms as configured when data for the instrument was first ingested."""

    __tablename__ = "instruments"

    instrument_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    tick_size: Mapped[Decimal]
    contract_size: Mapped[Decimal]
    quote_ccy: Mapped[str] = mapped_column(String(8))
    lot_step: Mapped[Decimal]
    min_lot: Mapped[Decimal]
    max_lot: Mapped[Decimal]
    venue: Mapped[str | None] = mapped_column(String(64))


class IngestRun(Base):
    """One execution of `xq ingest`."""

    __tablename__ = "ingest_runs"

    run_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    started_at: Mapped[pd.Timestamp]
    finished_at: Mapped[pd.Timestamp | None]
    status: Mapped[str] = mapped_column(String(16))
    git_sha: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    params_json: Mapped[dict[str, Any]]


class RawFile(Base):
    """An immutable source file in the raw store; this table is the raw-store manifest."""

    __tablename__ = "raw_files"

    raw_file_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"))
    path: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    row_count: Mapped[int] = mapped_column(BigInteger)
    first_ts_utc: Mapped[pd.Timestamp | None]
    last_ts_utc: Mapped[pd.Timestamp | None]
    ingest_run_id: Mapped[str] = mapped_column(ForeignKey("ingest_runs.run_id"))
    ingested_at: Mapped[pd.Timestamp]


class CleanPartition(Base):
    """One trading day of clean ticks built with one cleaning-rules version (DATA-007)."""

    __tablename__ = "clean_partitions"

    partition_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"))
    trading_day: Mapped[date] = mapped_column(Date)
    rules_version: Mapped[str] = mapped_column(String(32))
    raw_file_ids: Mapped[list[str]]
    row_count: Mapped[int] = mapped_column(BigInteger)
    flagged_count: Mapped[int] = mapped_column(BigInteger)
    dropped_count: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))


class CleaningAction(Base):
    """A logged cleaning action with the original values it affected (DATA-007)."""

    __tablename__ = "cleaning_actions"

    action_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partition_id: Mapped[str] = mapped_column(ForeignKey("clean_partitions.partition_id"))
    rule_id: Mapped[str] = mapped_column(String(64))
    ts_utc: Mapped[pd.Timestamp]
    action: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    original_values_json: Mapped[dict[str, Any]]


class BarSet(Base):
    """A built set of bars for one source, instrument, timeframe and price basis (DATA-008)."""

    __tablename__ = "bar_sets"

    bar_set_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"))
    timeframe: Mapped[str] = mapped_column(String(8))
    basis: Mapped[str] = mapped_column(String(8))
    clean_rules_version: Mapped[str] = mapped_column(String(32))
    build_version: Mapped[str] = mapped_column(String(32))
    start_utc: Mapped[pd.Timestamp]
    end_utc: Mapped[pd.Timestamp]
    row_count: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))


class BarGap(Base):
    """An interval without bars; `expected_open` marks gaps while the market should be open."""

    __tablename__ = "bar_gaps"

    bar_set_id: Mapped[str] = mapped_column(ForeignKey("bar_sets.bar_set_id"), primary_key=True)
    gap_start_utc: Mapped[pd.Timestamp] = mapped_column(primary_key=True)
    gap_end_utc: Mapped[pd.Timestamp]
    expected_open: Mapped[bool] = mapped_column(Boolean)


class SpreadStat(Base):
    """Spread percentiles per hour of week, used by the cost-model fallback (DATA-009)."""

    __tablename__ = "spread_stats"

    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(
        ForeignKey("instruments.instrument_id"), primary_key=True
    )
    hour_of_week: Mapped[int] = mapped_column(Integer, primary_key=True)
    computed_from: Mapped[pd.Timestamp] = mapped_column(primary_key=True)
    computed_to: Mapped[pd.Timestamp] = mapped_column(primary_key=True)
    p50: Mapped[float] = mapped_column(Float)
    p90: Mapped[float] = mapped_column(Float)
    p99: Mapped[float] = mapped_column(Float)
    n: Mapped[int] = mapped_column(BigInteger)


class QualityRunRecord(Base):
    """One `xq validate` run over a source and window (DQ-006)."""

    __tablename__ = "quality_runs"

    run_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    start_utc: Mapped[pd.Timestamp]
    end_utc: Mapped[pd.Timestamp]
    rules_version: Mapped[str] = mapped_column(String(32))
    build_version: Mapped[str] = mapped_column(String(32))
    includes_vault: Mapped[bool] = mapped_column(Boolean)
    git_sha: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    report_path: Mapped[str] = mapped_column(Text)
    summary_json: Mapped[dict[str, Any]]
    created_at: Mapped[pd.Timestamp]


class QualityResultRecord(Base):
    """One graded check on one partition (trading day) of a quality run."""

    __tablename__ = "quality_results"

    run_id: Mapped[str] = mapped_column(ForeignKey("quality_runs.run_id"), primary_key=True)
    partition_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    check_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trading_day: Mapped[date] = mapped_column(Date)
    severity: Mapped[str] = mapped_column(String(16))
    metric_value: Mapped[float] = mapped_column(Float)
    warn_threshold: Mapped[float | None] = mapped_column(Float)
    fail_threshold: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(8))
    details_json: Mapped[dict[str, Any]]
