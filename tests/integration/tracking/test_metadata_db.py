"""DATA-005: metadata database models and migrations."""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from xq.core.errors import ConfigError, NaiveTimestampError
from xq.tracking.db import (
    create_db_engine,
    current_revision,
    downgrade_to,
    head_revision,
    session_factory,
    upgrade_to_head,
)
from xq.tracking.models import Base, DataSource, IngestRun, Instrument, RawFile

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"
TABLES = {
    "data_sources",
    "instruments",
    "ingest_runs",
    "raw_files",
    "clean_partitions",
    "cleaning_actions",
    "bar_sets",
    "bar_gaps",
    "spread_stats",
    "quality_runs",
    "quality_results",
    "vault_tokens",
    "models",
    "model_versions",
    "status_history",
    "gate_results",
    "strategy_bundles",
    "active_bundles",
    "bundle_performance",
    "vault_access_log",
    "dataset_versions",
    "hypotheses",
    "experiments",
    "runs",
    "trials",
    "metrics",
    "artifacts",
    "target_sets",
    "fold_results",
    "conclusions",
    "backtests",
    "stat_tests",
    "robustness_results",
}
LATEST = "0016"
NOW = pd.Timestamp("2026-09-26 01:00:00.123456789", tz="UTC")


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'nested' / 'metadata.sqlite'}")
    upgrade_to_head(engine, MIGRATIONS)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with session_factory(engine)() as session:
        yield session


def _seed(session: Session) -> None:
    session.add_all(
        [
            DataSource(
                source_id="mt5_primary",
                vendor="broker",
                feed_type="broker_ticks",
                venue="tbd",
                price_type="bid_ask_ticks",
                clock_convention="NY+7",
                notes="",
            ),
            Instrument(
                instrument_id="xauusd",
                symbol="XAUUSD",
                tick_size=Decimal("0.01"),
                contract_size=Decimal("100"),
                quote_ccy="USD",
                lot_step=Decimal("0.01"),
                min_lot=Decimal("0.01"),
                max_lot=Decimal("100"),
                venue=None,
            ),
            IngestRun(
                run_id="01J0000000000000000000000A",
                started_at=NOW,
                finished_at=None,
                status="running",
                git_sha="abc",
                config_hash="def",
                params_json={"path": "x"},
            ),
        ]
    )
    session.commit()


def _raw_file(raw_file_id: str, sha256: str, source_id: str = "mt5_primary") -> RawFile:
    return RawFile(
        raw_file_id=raw_file_id,
        source_id=source_id,
        instrument_id="xauusd",
        path=f"data/raw/{raw_file_id}.csv",
        original_name="ticks.csv",
        sha256=sha256,
        bytes=10,
        row_count=2,
        first_ts_utc=NOW,
        last_ts_utc=NOW + pd.Timedelta(seconds=1),
        ingest_run_id="01J0000000000000000000000A",
        ingested_at=NOW,
    )


def test_upgrade_creates_every_table_and_downgrade_removes_them(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    assert current_revision(engine) is None

    upgrade_to_head(engine, MIGRATIONS)
    assert current_revision(engine) == head_revision(MIGRATIONS) == LATEST
    assert set(inspect(engine).get_table_names()) >= TABLES

    upgrade_to_head(engine, MIGRATIONS)  # idempotent
    assert current_revision(engine) == LATEST

    downgrade_to(engine, MIGRATIONS, "base")
    assert current_revision(engine) is None
    assert not TABLES & set(inspect(engine).get_table_names())

    upgrade_to_head(engine, MIGRATIONS)
    assert set(inspect(engine).get_table_names()) >= TABLES
    engine.dispose()


def test_migrations_match_the_models(engine: Engine) -> None:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_timestamps_and_decimals_round_trip_exactly(session: Session) -> None:
    _seed(session)
    session.add(_raw_file("rf1", "a" * 64))
    session.commit()
    session.expunge_all()

    raw = session.scalars(select(RawFile)).one()
    assert raw.first_ts_utc == NOW  # nanoseconds preserved
    assert raw.first_ts_utc is not None
    assert str(raw.first_ts_utc.tz) == "UTC"
    instrument = session.get(Instrument, "xauusd")
    assert instrument is not None
    assert instrument.tick_size == Decimal("0.01")


def test_timestamps_are_stored_as_int64_nanoseconds(engine: Engine, session: Session) -> None:
    _seed(session)
    with engine.connect() as connection:
        stored = connection.exec_driver_sql("SELECT started_at FROM ingest_runs").scalar_one()
    assert stored == NOW.value


def test_naive_timestamps_are_rejected(session: Session) -> None:
    _seed(session)
    raw = _raw_file("rf1", "a" * 64)
    raw.ingested_at = pd.Timestamp("2026-09-26 01:00")
    session.add(raw)
    with pytest.raises(StatementError) as excinfo:
        session.commit()
    assert isinstance(excinfo.value.orig, NaiveTimestampError)


def test_foreign_keys_are_enforced(session: Session) -> None:
    _seed(session)
    session.add(_raw_file("rf1", "a" * 64, source_id="unknown_source"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_raw_file_sha256_is_unique(session: Session) -> None:
    _seed(session)
    session.add(_raw_file("rf1", "a" * 64))
    session.commit()
    session.add(_raw_file("rf2", "a" * 64))
    with pytest.raises(IntegrityError):
        session.commit()


def test_missing_migrations_directory_is_a_config_error(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    with pytest.raises(ConfigError, match="migrations directory not found"):
        upgrade_to_head(engine, tmp_path / "nowhere")
    engine.dispose()
