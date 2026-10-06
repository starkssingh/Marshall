"""ADR 0071: a re-exported month supersedes the earlier raw file of the same source and period.

Raw data stays immutable: both files remain in the raw store and the manifest, and `verify-raw`
checks both. Clean, bars and the quality checks read the superseding file for the period and
never the superseded one, so a trading day is never built from both.
"""

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from helpers.dukascopy_fixtures import synthetic_quotes, to_csv_text
from helpers.pipeline import config
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.core.errors import SupersessionError
from xq.data.bars import build_bar_sets
from xq.data.clean import build_clean, clean_partition_path
from xq.data.flags import TickFlag
from xq.data.raw_store import (
    active_raw_files,
    ingest,
    raw_file_id_for,
    sha256_file,
    verify_raw_store,
)
from xq.tracking.db import create_db_engine, session_factory, upgrade_to_head
from xq.tracking.models import CleanPartition, IngestRun, RawFile, RawFileSupersession

REPO = Path(__file__).resolve().parents[3]
SOURCE = "dukascopy"
HOLE = ("2024-10-30 10:00", "2024-10-30 11:00")  # a whole UTC hour the first export skipped
DAMAGED_DAY = date(2024, 10, 30)  # trading day 2024-10-29 21:00 to 2024-10-30 21:00 UTC (EDT)
BOUNDARY_DAY = date(2024, 11, 1)  # 2024-10-31 21:00 to 2024-11-01 21:00 UTC: both months
REASON = "re-export of 2024-10: the first export skipped whole UTC hours (DQ-008 review 5.3)"


def quotes(start: str, end: str, seed: int) -> pd.DataFrame:
    return synthetic_quotes(start, end, seed=seed, start_price=2750.0, mean_interval_s=20.0)


def write(directory: Path, name: str, frame: pd.DataFrame) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(to_csv_text(frame))
    return path


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    yield engine
    engine.dispose()


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    october = quotes("2024-10-27 00:00", "2024-11-01 00:00", seed=71)
    hole = october["ts_utc"].between(
        pd.Timestamp(HOLE[0], tz="UTC"), pd.Timestamp(HOLE[1], tz="UTC"), inclusive="left"
    )
    assert hole.sum() > 50
    return {
        "damaged": write(tmp_path / "first", "XAUUSD_ticks_2024-10.csv", october[~hole]),
        "november": write(
            tmp_path / "first",
            "XAUUSD_ticks_2024-11.csv",
            quotes("2024-11-01 00:00", "2024-11-02 00:00", seed=72),
        ),
        "reexport": write(tmp_path / "reexport", "XAUUSD_ticks_2024-10.csv", october),
    }


def run_ingest(cfg: AppConfig, engine: Engine, path: Path, run_id: str, **kwargs: Any) -> list[str]:
    return ingest(
        cfg, SOURCE, path, engine=engine, run_id=run_id, git_sha="test", **kwargs
    ).ingested


def first_export(cfg: AppConfig, engine: Engine, files: dict[str, Path]) -> str:
    run_ingest(cfg, engine, files["damaged"].parent, "01RUNA0000000000000000000A")
    return raw_file_id_for(sha256_file(files["damaged"]))


def clean_day(cfg: AppConfig, version: str, day: date) -> pd.DataFrame:
    return pd.read_parquet(clean_partition_path(cfg, SOURCE, day, version))


def partition(engine: Engine, day: date) -> CleanPartition:
    with session_factory(engine)() as session:
        found = session.scalar(select(CleanPartition).where(CleanPartition.trading_day == day))
        assert found is not None
        return found


def in_hole(ticks: pd.DataFrame) -> int:
    lo, hi = (pd.Timestamp(t, tz="UTC").value for t in HOLE)
    return int(ticks["ts_utc"].between(lo, hi, inclusive="left").sum())


def test_the_reexport_replaces_the_damaged_file_and_the_two_are_never_mixed(
    cfg: AppConfig, engine: Engine, files: dict[str, Path]
) -> None:
    old = first_export(cfg, engine, files)
    first = build_clean(cfg, engine, SOURCE)
    assert in_hole(clean_day(cfg, first.rules_version, DAMAGED_DAY)) == 0

    (new,) = run_ingest(
        cfg,
        engine,
        files["reexport"],
        "01RUNB0000000000000000000B",
        supersedes=[old],
        reason=REASON,
    )
    second = build_clean(cfg, engine, SOURCE)
    assert DAMAGED_DAY in second.built  # its contributing files changed, so it was rebuilt

    november = raw_file_id_for(sha256_file(files["november"]))
    assert partition(engine, DAMAGED_DAY).raw_file_ids == [new]
    assert partition(engine, BOUNDARY_DAY).raw_file_ids == sorted([new, november])

    ticks = clean_day(cfg, second.rules_version, DAMAGED_DAY)
    assert set(ticks["raw_file_id"]) == {new}
    assert in_hole(ticks) > 50  # the hole is filled
    duplicates = (ticks["flags"].to_numpy() & np.uint32(TickFlag.DUP_EXACT)) != 0
    assert not duplicates.any()  # the identical ticks of the old file are not read too
    boundary = clean_day(cfg, second.rules_version, BOUNDARY_DAY)
    assert old not in set(boundary["raw_file_id"])
    assert not ((boundary["flags"].to_numpy() & np.uint32(TickFlag.DUP_EXACT)) != 0).any()

    built = build_bar_sets(cfg, engine, SOURCE)
    assert built.build_version


def test_the_supersession_is_recorded_with_its_provenance(
    cfg: AppConfig, engine: Engine, files: dict[str, Path]
) -> None:
    old = first_export(cfg, engine, files)
    (new,) = run_ingest(
        cfg,
        engine,
        files["reexport"],
        "01RUNB0000000000000000000B",
        supersedes=[old],
        reason=REASON,
    )
    with session_factory(engine)() as session:
        record = session.get(RawFileSupersession, old)
        assert record is not None
        assert (record.superseding_raw_file_id, record.source_id) == (new, SOURCE)
        assert record.reason == REASON
        assert record.ingest_run_id == "01RUNB0000000000000000000B"
        damaged = session.get(RawFile, old)
        assert damaged is not None
        assert (record.period_start_utc, record.period_end_utc) == (
            damaged.first_ts_utc,
            damaged.last_ts_utc,
        )
        run = session.get(IngestRun, "01RUNB0000000000000000000B")
        assert run is not None
        assert run.params_json["supersedes"] == [old]
        assert run.params_json["reason"] == REASON
        assert {r.raw_file_id for r in active_raw_files(session, SOURCE)} == {
            new,
            raw_file_id_for(sha256_file(files["november"])),
        }
        assert len(list(session.scalars(select(RawFile)))) == 3  # nothing left the manifest


def test_verify_raw_still_covers_the_superseded_file(
    cfg: AppConfig, engine: Engine, files: dict[str, Path]
) -> None:
    old = first_export(cfg, engine, files)
    run_ingest(
        cfg,
        engine,
        files["reexport"],
        "01RUNB0000000000000000000B",
        supersedes=[old],
        reason=REASON,
    )
    assert verify_raw_store(cfg, engine) == []
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)
    with session_factory(engine)() as session:
        damaged = session.get(RawFile, old)
        assert damaged is not None
        stored = data_dir / damaged.path
    assert stored.is_file()
    stored.chmod(0o644)
    stored.write_text(stored.read_text() + "\n")
    problems = verify_raw_store(cfg, engine)
    assert [(p.raw_file_id, p.problem) for p in problems] == [
        (old, "sha256 mismatch"),
        (old, "writable"),
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"reason": None}, "needs a reason"),
        ({"reason": "   "}, "needs a reason"),
        ({"supersedes": ["0123456789abcdef"]}, "unknown raw file"),
        ({"path": "first"}, "exactly one re-exported file"),
        ({"path": "damaged"}, "byte-identical"),
        ({"path": "elsewhere"}, "does not overlap"),
    ],
)
def test_malformed_supersessions_are_refused_and_store_nothing(
    cfg: AppConfig,
    engine: Engine,
    files: dict[str, Path],
    tmp_path: Path,
    change: dict[str, Any],
    message: str,
) -> None:
    old = first_export(cfg, engine, files)
    elsewhere = write(
        tmp_path / "elsewhere", "XAUUSD_ticks_2024-12.csv", quotes("2024-12-02", "2024-12-04", 73)
    )
    paths = {
        "first": files["damaged"].parent,
        "damaged": files["damaged"],
        "elsewhere": elsewhere,
    }
    request: dict[str, Any] = {"supersedes": [old], "reason": REASON, **change}
    path = paths.get(request.pop("path", ""), files["reexport"])
    with pytest.raises(SupersessionError, match=message):
        run_ingest(cfg, engine, path, "01RUNB0000000000000000000B", **request)
    with session_factory(engine)() as session:
        assert session.scalars(select(RawFileSupersession)).first() is None
        assert {r.raw_file_id for r in session.scalars(select(RawFile))} == {
            old,
            raw_file_id_for(sha256_file(files["november"])),
        }
    raw_root = cfg.paths.resolve(cfg.paths.data_dir) / "raw"
    assert not [p for p in raw_root.rglob("*") if p.is_file() and ".incoming" in p.parts]


def test_a_file_is_superseded_once_and_only_within_its_source(
    cfg: AppConfig, engine: Engine, files: dict[str, Path], tmp_path: Path
) -> None:
    old = first_export(cfg, engine, files)
    (new,) = run_ingest(
        cfg,
        engine,
        files["reexport"],
        "01RUNB0000000000000000000B",
        supersedes=[old],
        reason=REASON,
    )
    october = quotes("2024-10-27 00:00", "2024-11-01 00:00", seed=74)
    third = write(tmp_path / "third", "XAUUSD_ticks_2024-10.csv", october)
    with pytest.raises(SupersessionError, match=f"already superseded by {new}"):
        run_ingest(cfg, engine, third, "01RUNC0000000000000000000C", supersedes=[old], reason="x")
    # A second re-export supersedes the first one, never the original again.
    (newest,) = run_ingest(
        cfg, engine, third, "01RUNC0000000000000000000C", supersedes=[new], reason="again"
    )
    with session_factory(engine)() as session:
        active = {r.raw_file_id for r in active_raw_files(session, SOURCE)}
    assert newest in active
    assert not {old, new} & active

    with pytest.raises(SupersessionError, match="belongs to source 'dukascopy'"):
        ingest(
            cfg,
            "mt5_primary",
            REPO / "tests" / "fixtures" / "ticks" / "XAUUSD_mt5_ticks_2024-10-30_2024-11-06.csv",
            engine=engine,
            run_id="01RUND0000000000000000000D",
            git_sha="test",
            supersedes=[newest],
            reason="wrong source",
        )


def test_cli_ingest_with_supersedes(tmp_path: Path, files: dict[str, Path]) -> None:
    runner = CliRunner()
    base = [
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
    first = runner.invoke(app, [*base, "ingest", "--path", str(files["damaged"])])
    assert first.exit_code == 0, first.output
    old = raw_file_id_for(sha256_file(files["damaged"]))
    no_reason = runner.invoke(
        app, [*base, "ingest", "--path", str(files["reexport"]), "--supersedes", old]
    )
    assert no_reason.exit_code == 2
    assert "needs a reason" in no_reason.output
    done = runner.invoke(
        app,
        [
            *base,
            "ingest",
            "--path",
            str(files["reexport"]),
            "--supersedes",
            old,
            "--reason",
            REASON,
        ],
    )
    assert done.exit_code == 0, done.output
    assert f"superseded {old}" in done.output
    verified = runner.invoke(app, [*base, "verify-raw"])
    assert verified.exit_code == 0, verified.output
