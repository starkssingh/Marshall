"""DATA-007: per-trading-day clean partitions built from the raw mirror."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
import pytest
from sqlalchemy import Engine, func, select
from typer.testing import CliRunner

from helpers.mt5_fixtures import FIXTURE_DIR
from xq.cli.main import app
from xq.core.config import AppConfig, load_config
from xq.core.errors import MirrorVersionError
from xq.core.time import from_ns, trading_day
from xq.data.adapters import RawFileRef, build_adapter, validate_tick_frame
from xq.data.clean import build_clean, clean_partition_path, clean_rules_version
from xq.data.raw_store import ingest, rebuild_mirror, sha256_file
from xq.tracking.db import create_db_engine, session_factory, upgrade_to_head
from xq.tracking.models import CleaningAction, CleanPartition, RawFile

REPO = Path(__file__).resolve().parents[3]
MARCH = "XAUUSD_mt5_ticks_2024-03-06_2024-03-13.csv"


def make_config(root: Path, **overrides: Any) -> AppConfig:
    return load_config(
        "research",
        {"paths.root": str(root), "logging.file": None, "logging.console": False, **overrides},
        config_dir=REPO / "config",
    )


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return make_config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    yield engine
    engine.dispose()


def ingest_dir(cfg: AppConfig, engine: Engine, path: Path, run_id: str) -> None:
    ingest(cfg, "mt5_primary", path, engine=engine, run_id=run_id, git_sha="test")


def partitions(engine: Engine) -> list[CleanPartition]:
    with session_factory(engine)() as session:
        return list(session.scalars(select(CleanPartition).order_by(CleanPartition.trading_day)))


def test_mirror_carries_the_canonical_view(cfg: AppConfig, engine: Engine) -> None:
    ingest_dir(cfg, engine, FIXTURE_DIR, "01RUNA0000000000000000000A")
    adapter = build_adapter(cfg, "mt5_primary")
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)
    with session_factory(engine)() as session:
        record = session.scalars(select(RawFile).where(RawFile.original_name == MARCH)).one()
    stored = data_dir / record.path
    expected = adapter.to_canonical(
        adapter.read(RawFileRef(stored, MARCH, stored.stat().st_size))
    ).set_index("row_num")
    parts = sorted((data_dir / "raw_parquet").rglob(f"part-{record.raw_file_id}.parquet"))
    mirror = pd.concat(pq.read_table(p).to_pandas() for p in parts).set_index("row_num")
    mirror = mirror.loc[expected.index]
    for column in ("bid", "ask", "flags"):
        assert (mirror[f"c_{column}"].to_numpy() == expected[column].to_numpy()).all(), column
    assert int(pq.read_schema(parts[0]).metadata[b"xq.mirror_version"]) == 2


def test_build_writes_one_partition_per_trading_day(cfg: AppConfig, engine: Engine) -> None:
    ingest_dir(cfg, engine, FIXTURE_DIR, "01RUNA0000000000000000000A")
    result = build_clean(cfg, engine, "mt5_primary")
    version = clean_rules_version(cfg)
    assert result.rules_version == version
    assert result.rows == 9248
    assert result.dropped == 0

    rows = partitions(engine)
    assert [p.trading_day for p in rows] == result.built
    assert len(rows) == 12
    total_flagged = 0
    for partition in rows:
        path = clean_partition_path(cfg, "mt5_primary", partition.trading_day, version)
        assert sha256_file(path) == partition.sha256
        assert partition.rules_version == version
        ticks = validate_tick_frame(pd.read_parquet(path))
        assert len(ticks) == partition.row_count
        assert {trading_day(from_ns(int(t))) for t in ticks["ts_utc"]} == {partition.trading_day}
        assert pq.read_schema(path).metadata[b"xq.rules_version"] == version.encode()
        total_flagged += partition.flagged_count
    assert total_flagged == result.flagged
    with session_factory(engine)() as session:
        actions = session.scalar(select(func.count()).select_from(CleaningAction))
    assert actions == total_flagged  # flags logged, nothing dropped


def test_rebuild_is_skipped_or_bit_identical(cfg: AppConfig, engine: Engine) -> None:
    ingest_dir(cfg, engine, FIXTURE_DIR, "01RUNA0000000000000000000A")
    build_clean(cfg, engine, "mt5_primary")
    before = {p.trading_day: p.sha256 for p in partitions(engine)}

    again = build_clean(cfg, engine, "mt5_primary")
    assert again.built == []
    assert len(again.skipped) == 12

    forced = build_clean(cfg, engine, "mt5_primary", force=True)
    assert len(forced.built) == 12
    assert {p.trading_day: p.sha256 for p in partitions(engine)} == before


def test_overlapping_export_is_flagged_and_can_be_dropped(tmp_path: Path, engine: Engine) -> None:
    overlap_dir = tmp_path / "overlap"
    overlap_dir.mkdir()
    lines = (FIXTURE_DIR / MARCH).read_text().splitlines(keepends=True)
    (overlap_dir / "XAUUSD_partial_export.csv").write_text("".join(lines[:201]))

    cfg = make_config(tmp_path, **{"cleaning.drop": ["DUP_EXACT"]})
    ingest_dir(cfg, engine, FIXTURE_DIR, "01RUNA0000000000000000000A")
    build_clean(cfg, engine, "mt5_primary")
    ingest_dir(cfg, engine, overlap_dir, "01RUNB0000000000000000000B")
    result = build_clean(cfg, engine, "mt5_primary")

    assert result.built == [pd.Timestamp("2024-03-06").date()]  # only the affected day
    assert result.dropped == 200
    with session_factory(engine)() as session:
        drops = list(session.scalars(select(CleaningAction).where(CleaningAction.action == "drop")))
    assert len(drops) == 200
    assert {d.rule_id for d in drops} == {"DUP_EXACT"}
    assert all(d.original_values_json["bid"] is not None for d in drops)


def test_new_rules_version_builds_a_separate_store(tmp_path: Path, engine: Engine) -> None:
    cfg = make_config(tmp_path)
    ingest_dir(cfg, engine, FIXTURE_DIR, "01RUNA0000000000000000000A")
    first = build_clean(cfg, engine, "mt5_primary")
    stricter = make_config(tmp_path, **{"cleaning.stale.seconds": 60})
    second = build_clean(stricter, engine, "mt5_primary")
    assert second.rules_version != first.rules_version
    assert len(second.built) == 12
    assert second.flagged > first.flagged  # a shorter stale limit flags more
    assert len(partitions(engine)) == 24


def test_old_mirror_schema_is_refused_until_rebuilt(cfg: AppConfig, engine: Engine) -> None:
    ingest_dir(cfg, engine, FIXTURE_DIR, "01RUNA0000000000000000000A")
    part = next((cfg.paths.resolve(cfg.paths.data_dir) / "raw_parquet").rglob("*.parquet"))
    table = pq.read_table(part)
    pq.write_table(table.replace_schema_metadata({}), part)  # a mirror written before v2

    with pytest.raises(MirrorVersionError, match="rebuild-mirror"):
        build_clean(cfg, engine, "mt5_primary")
    assert rebuild_mirror(cfg, engine, "mt5_primary") == 2
    # Days committed before the refusal are still valid and skipped; the rest are built now.
    retried = build_clean(cfg, engine, "mt5_primary")
    assert retried.built
    assert len(retried.built) + len(retried.skipped) == 12


def test_cli_clean(tmp_path: Path) -> None:
    common = [
        "--config-dir",
        str(REPO / "config"),
        "--profile",
        "research",
        "--set",
        f"paths.root={tmp_path}",
        "--set",
        f"paths.migrations_dir={REPO / 'migrations'}",
        "--set",
        "logging.console=false",
    ]
    runner = CliRunner()
    ingested = runner.invoke(
        app, [*common, "ingest", "--source", "mt5_primary", "--path", str(FIXTURE_DIR)]
    )
    assert ingested.exit_code == 0, ingested.output
    first = runner.invoke(app, [*common, "clean", "--source", "mt5_primary"])
    assert first.exit_code == 0, first.output
    assert "built 12 trading day(s) with 9248 ticks" in first.stdout
    ranged = runner.invoke(
        app,
        [
            *common,
            "clean",
            "--source",
            "mt5_primary",
            "--start",
            "2024-03-11",
            "--end",
            "2024-03-12",
            "--force",
        ],
    )
    assert ranged.exit_code == 0, ranged.output
    assert "built 2 trading day(s)" in ranged.stdout
