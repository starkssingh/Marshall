"""DATA-004: immutable raw store, Parquet mirror, manifest rows and idempotent re-ingest."""

import json
import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest
import structlog.testing
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from helpers.mt5_fixtures import FIXTURE_DIR, FIXTURES
from xq.cli.main import app
from xq.core.config import AppConfig, load_config
from xq.core.errors import ProvenanceError, RawStoreIntegrityError, SourceFormatError
from xq.data.adapters import RawFileRef, build_adapter
from xq.data.raw_store import IngestResult, ingest, sha256_file, verify_raw_store
from xq.tracking.db import create_db_engine, session_factory, upgrade_to_head
from xq.tracking.models import DataSource, IngestRun, RawFile

REPO = Path(__file__).resolve().parents[3]
REPO_CONFIG = REPO / "config"
MIGRATIONS = REPO / "migrations"
FIXTURE_NAMES = sorted(FIXTURES)


def make_config(root: Path, **overrides: str) -> AppConfig:
    return load_config(
        "research",
        {"paths.root": str(root), "logging.file": None, "logging.console": False, **overrides},
        config_dir=REPO_CONFIG,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return make_config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, MIGRATIONS)
    yield engine
    engine.dispose()


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "incoming"
    shutil.copytree(FIXTURE_DIR, directory, ignore=shutil.ignore_patterns("*.md"))
    return directory


def run(cfg: AppConfig, engine: Engine, path: Path, run_id: str) -> IngestResult:
    return ingest(cfg, "mt5_primary", path, engine=engine, run_id=run_id, git_sha="test-sha")


def rows(engine: Engine) -> list[RawFile]:
    with session_factory(engine)() as session:
        return list(session.scalars(select(RawFile).order_by(RawFile.original_name)))


def test_ingest_stores_read_only_originals_mirror_and_manifest(
    cfg: AppConfig, engine: Engine, source_dir: Path
) -> None:
    result = run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)

    records = rows(engine)
    assert [r.original_name for r in records] == FIXTURE_NAMES
    assert sorted(result.ingested) == sorted(r.raw_file_id for r in records)
    assert result.skipped == []
    assert result.rows == sum(r.row_count for r in records)

    for record in records:
        original = source_dir / record.original_name
        stored = data_dir / record.path
        assert stored.read_bytes() == original.read_bytes()
        assert record.sha256 == sha256_file(original)
        assert record.raw_file_id == record.sha256[:16]
        assert stored.name == f"{record.raw_file_id}__{record.original_name}"
        assert stat.S_IMODE(stored.stat().st_mode) == 0o444
        assert record.row_count == len(original.read_text().splitlines()) - 1
        assert record.ingest_run_id == "01RUNA0000000000000000000A"
        assert record.first_ts_utc is not None
        assert record.last_ts_utc is not None
        assert record.path.startswith(f"raw/mt5_primary/xauusd/{record.first_ts_utc:%Y/%m}/")

    march = next(r for r in records if "2024-03-06" in r.original_name)
    assert march.first_ts_utc == pd.Timestamp("2024-03-06 00:00:54.540", tz="UTC")

    with session_factory(engine)() as session:
        ingest_run = session.get(IngestRun, "01RUNA0000000000000000000A")
        source = session.get(DataSource, "mt5_primary")
    assert ingest_run is not None
    assert ingest_run.status == "succeeded"
    assert ingest_run.git_sha == "test-sha"
    assert ingest_run.params_json["ingested"] == 2
    assert source is not None
    assert source.clock_convention == "NY+7"


def test_mirror_is_faithful_and_partitioned_by_utc_day(
    cfg: AppConfig, engine: Engine, source_dir: Path
) -> None:
    run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)
    adapter = build_adapter(cfg, "mt5_primary")

    for record in rows(engine):
        parts = sorted((data_dir / "raw_parquet").rglob(f"part-{record.raw_file_id}.parquet"))
        mirror = pd.concat([pq.read_table(p).to_pandas() for p in parts]).sort_values("row_num")
        assert mirror["row_num"].tolist() == list(range(record.row_count))
        assert (mirror["raw_file_id"] == record.raw_file_id).all()

        stored = data_dir / record.path
        raw = adapter.read(RawFileRef(stored, record.original_name, stored.stat().st_size))
        assert mirror["ts_raw"].tolist() == raw["ts_raw"].tolist()
        pd.testing.assert_series_equal(
            mirror["bid"].reset_index(drop=True), raw["bid"], check_names=False
        )
        assert mirror["ts_utc"].tolist() == adapter.timestamps(raw).ts_utc.tolist()

        for part in parts:
            day = pd.read_parquet(part)
            stamps = pd.to_datetime(day["ts_utc"], unit="ns", utc=True)
            folder = part.parent
            expected = f"year={stamps.dt.year.iloc[0]:04d}/month={stamps.dt.month.iloc[0]:02d}"
            assert expected in folder.as_posix()
            assert folder.name == f"day={stamps.dt.day.iloc[0]:02d}"
            assert stamps.dt.date.nunique() == 1


def test_second_ingest_is_a_no_op(cfg: AppConfig, engine: Engine, source_dir: Path) -> None:
    run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)
    before = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for p in data_dir.rglob("*")
        if p.is_file() and p.name != "metadata.sqlite"
    }

    again = run(cfg, engine, source_dir, "01RUNB0000000000000000000B")
    assert again.ingested == []
    assert len(again.skipped) == 2
    assert len(rows(engine)) == 2
    after = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for p in data_dir.rglob("*")
        if p.is_file() and p.name != "metadata.sqlite"
    }
    assert after == before


def test_same_content_under_another_name_is_skipped(
    cfg: AppConfig, engine: Engine, source_dir: Path, tmp_path: Path
) -> None:
    run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    renamed = tmp_path / "renamed"
    renamed.mkdir()
    shutil.copy(source_dir / FIXTURE_NAMES[0], renamed / "copy_of_march.csv")
    result = run(cfg, engine, renamed, "01RUNB0000000000000000000B")
    assert result.ingested == []
    assert len(result.skipped) == 1


def test_same_file_under_another_source_warns_naming_both(
    tmp_path: Path, cfg: AppConfig, engine: Engine, source_dir: Path
) -> None:
    run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    primary = cfg.source("mt5_primary").model_dump()
    other = make_config(tmp_path, **{f"sources.mt5_other.{k}": v for k, v in primary.items()})

    with structlog.testing.capture_logs() as events:
        result = ingest(
            other,
            "mt5_other",
            source_dir,
            engine=engine,
            run_id="01RUNB0000000000000000000B",
            git_sha="test-sha",
        )
    assert result.ingested == []
    assert len(result.skipped) == 2
    warnings = [e for e in events if e["log_level"] == "warning"]
    assert len(warnings) == 2
    for warning in warnings:
        assert warning["event"] == "raw_file_already_ingested_under_other_source"
        assert warning["source_id"] == "mt5_other"
        assert warning["existing_source_id"] == "mt5_primary"
    # Same-source re-ingest stays a quiet skip.
    with structlog.testing.capture_logs() as events:
        run(cfg, engine, source_dir, "01RUNC0000000000000000000C")
    assert not [e for e in events if e["log_level"] == "warning"]


def test_verify_detects_modified_raw_files(
    cfg: AppConfig, engine: Engine, source_dir: Path
) -> None:
    run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    assert verify_raw_store(cfg, engine) == []

    stored = cfg.paths.resolve(cfg.paths.data_dir) / rows(engine)[0].path
    stored.chmod(0o644)
    with stored.open("a") as handle:
        handle.write("2024.03.12\t23:59:59.999\t1.00\t1.01\t\t\t6\n")
    problems = {p.problem for p in verify_raw_store(cfg, engine)}
    assert problems == {"sha256 mismatch", "writable"}

    stored.unlink()
    assert [p.problem for p in verify_raw_store(cfg, engine)] == ["missing"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_raw_files_cannot_be_written(cfg: AppConfig, engine: Engine, source_dir: Path) -> None:
    run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    stored = cfg.paths.resolve(cfg.paths.data_dir) / rows(engine)[0].path
    with pytest.raises(PermissionError):
        stored.open("a")


def test_interrupted_run_is_recovered(cfg: AppConfig, engine: Engine, source_dir: Path) -> None:
    run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    records = rows(engine)
    # Simulate a crash after the copy but before the manifest row: drop the rows, keep the files.
    with session_factory(engine)() as session:
        for record in session.scalars(select(RawFile)):
            session.delete(record)
        session.commit()
    result = run(cfg, engine, source_dir, "01RUNB0000000000000000000B")
    assert sorted(result.ingested) == sorted(r.raw_file_id for r in records)
    assert verify_raw_store(cfg, engine) == []


def test_conflicting_stored_file_is_refused(
    cfg: AppConfig, engine: Engine, source_dir: Path
) -> None:
    run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    record = rows(engine)[0]
    stored = cfg.paths.resolve(cfg.paths.data_dir) / record.path
    with session_factory(engine)() as session:
        session.delete(session.get(RawFile, record.raw_file_id))
        session.commit()
    stored.chmod(0o644)
    stored.write_text("tampered")
    with pytest.raises(RawStoreIntegrityError, match="different content"):
        run(cfg, engine, source_dir, "01RUNB0000000000000000000B")


def test_changing_a_sources_clock_after_ingest_is_refused(
    tmp_path: Path, engine: Engine, cfg: AppConfig, source_dir: Path
) -> None:
    run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    changed = make_config(tmp_path, **{"sources.mt5_primary.clock": "tz:Europe/Athens"})
    with pytest.raises(ProvenanceError, match="clock_convention: 'NY\\+7' -> 'tz:Europe/Athens'"):
        run(changed, engine, source_dir, "01RUNB0000000000000000000B")


def test_failed_file_marks_the_run_failed_and_keeps_earlier_files(
    cfg: AppConfig, engine: Engine, source_dir: Path
) -> None:
    (source_dir / "zz_broken.csv").write_text("not an mt5 export\n")
    with pytest.raises(SourceFormatError):
        run(cfg, engine, source_dir, "01RUNA0000000000000000000A")
    assert len(rows(engine)) == 2  # the good files sort first and were committed
    with session_factory(engine)() as session:
        ingest_run = session.get(IngestRun, "01RUNA0000000000000000000A")
    assert ingest_run is not None
    assert ingest_run.status == "failed"


def test_cli_ingest_twice(tmp_path: Path) -> None:
    runner = CliRunner()
    args = [
        "--config-dir",
        str(REPO_CONFIG),
        "--profile",
        "research",
        "--set",
        f"paths.root={tmp_path}",
        "--set",
        f"paths.migrations_dir={MIGRATIONS}",
        "--set",
        "logging.console=false",
        "ingest",
        "--source",
        "mt5_primary",
        "--path",
        str(FIXTURE_DIR),
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "ingested 2 file(s) with 9248 rows; skipped 0" in first.stdout
    second = runner.invoke(app, args)
    assert second.exit_code == 0, second.output
    assert "ingested 0 file(s) with 0 rows; skipped 2" in second.stdout

    log_lines = [
        json.loads(line) for line in (tmp_path / "logs" / "xq.jsonl").read_text().splitlines()
    ]
    assert {line["event"] for line in log_lines} >= {
        "ingest_started",
        "raw_file_ingested",
        "raw_file_skipped",
        "ingest_finished",
    }
    assert all("run_id" in line and "git_sha" in line for line in log_lines)

    verify = runner.invoke(app, [*args[:10], "verify-raw"])
    assert verify.exit_code == 0, verify.output


def test_cli_ingest_unknown_source_is_a_usage_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "--config-dir",
            str(REPO_CONFIG),
            "--set",
            f"paths.root={tmp_path}",
            "ingest",
            "--source",
            "nope",
            "--path",
            str(FIXTURE_DIR),
        ],
    )
    assert result.exit_code == 2
    assert "unknown source 'nope'" in result.stderr
