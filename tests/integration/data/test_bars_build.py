"""DATA-008: bar sets built from clean partitions, with gaps, provenance and exact rebuilds."""

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from helpers.mt5_fixtures import FIXTURE_DIR
from xq.cli.main import app
from xq.core.config import AppConfig, load_config
from xq.core.types import Timeframe
from xq.data.bars import NoCleanDataError, bar_set_dir, bar_set_id, build_bar_sets
from xq.data.clean import build_clean
from xq.data.raw_store import ingest, sha256_file
from xq.tracking.db import create_db_engine, session_factory, upgrade_to_head
from xq.tracking.models import BarGap, BarSet

REPO = Path(__file__).resolve().parents[3]


def ns(text: str) -> int:
    return int(pd.Timestamp(text, tz="UTC").value)


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return load_config(
        "research",
        {"paths.root": str(tmp_path), "logging.file": None, "logging.console": False},
        config_dir=REPO / "config",
    )


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    ingest(
        cfg,
        "mt5_primary",
        FIXTURE_DIR,
        engine=engine,
        run_id="01RUNA0000000000000000000A",
        git_sha="test",
    )
    yield engine
    engine.dispose()


def load(cfg: AppConfig, tf: Timeframe, version: str) -> pd.DataFrame:
    return pd.read_parquet(bar_set_dir(cfg, "mt5_primary", tf, version)).sort_values(
        "bar_start_utc", ignore_index=True
    )


def test_bars_need_clean_partitions(cfg: AppConfig, engine: Engine) -> None:
    with pytest.raises(NoCleanDataError, match="xq clean"):
        build_bar_sets(cfg, engine, "mt5_primary")


def test_seven_timeframes_with_provenance(cfg: AppConfig, engine: Engine) -> None:
    clean = build_clean(cfg, engine, "mt5_primary")
    result = build_bar_sets(cfg, engine, "mt5_primary")
    assert result.clean_rules_version == clean.rules_version
    assert result.months == ["2024-03", "2024-10", "2024-11"]
    assert set(result.rows) == {tf.value for tf in Timeframe}

    with session_factory(engine)() as session:
        sets = {s.timeframe: s for s in session.scalars(select(BarSet))}
    assert set(sets) == {tf.value for tf in Timeframe}
    for tf in Timeframe:
        record = sets[tf.value]
        bars = load(cfg, tf, result.build_version)
        assert record.bar_set_id == bar_set_id("mt5_primary", "xauusd", tf, result.build_version)
        assert record.row_count == len(bars) == result.rows[tf.value]
        assert record.clean_rules_version == clean.rules_version
        assert record.basis == "bid+ask+mid"
        assert bars["tick_count"].sum() == clean.rows  # nothing excluded in this data
        assert (bars["bar_start_utc"].diff().dropna() > 0).all()


def test_daily_bars_follow_the_new_york_roll_across_dst(cfg: AppConfig, engine: Engine) -> None:
    build_clean(cfg, engine, "mt5_primary")
    version = build_bar_sets(cfg, engine, "mt5_primary").build_version
    daily = load(cfg, Timeframe.D1, version).set_index("trading_day")
    assert daily.loc[pd.Timestamp("2024-03-08").date(), "bar_start_utc"] == ns("2024-03-07 22:00")
    assert daily.loc[pd.Timestamp("2024-03-11").date(), "bar_start_utc"] == ns("2024-03-10 21:00")
    assert daily.loc[pd.Timestamp("2024-11-01").date(), "bar_start_utc"] == ns("2024-10-31 21:00")
    assert daily.loc[pd.Timestamp("2024-11-04").date(), "bar_start_utc"] == ns("2024-11-03 22:00")
    # Only the bar at the end of the data is still in progress.
    assert daily["is_complete"].tolist() == [True] * 11 + [False]


def test_no_bar_contains_a_tick_at_or_after_its_available_at(
    cfg: AppConfig, engine: Engine
) -> None:
    build_clean(cfg, engine, "mt5_primary")
    version = build_bar_sets(cfg, engine, "mt5_primary").build_version
    root = cfg.paths.resolve(cfg.paths.data_dir) / "clean"
    ts = np.sort(pd.read_parquet(root)["ts_utc"].to_numpy())
    for tf in Timeframe:
        bars = load(cfg, tf, version)
        starts = bars["bar_start_utc"].to_numpy()
        owner = np.searchsorted(starts, ts, side="right") - 1
        assert (ts >= starts[owner]).all()
        assert (ts < bars["available_at_utc"].to_numpy()[owner]).all(), tf


def test_gaps_mark_closed_market_periods_as_not_expected(cfg: AppConfig, engine: Engine) -> None:
    build_clean(cfg, engine, "mt5_primary")
    version = build_bar_sets(cfg, engine, "mt5_primary").build_version
    set_id = bar_set_id("mt5_primary", "xauusd", Timeframe.H1, version)
    with session_factory(engine)() as session:
        gaps = {
            (int(g.gap_start_utc.value), int(g.gap_end_utc.value)): g.expected_open
            for g in session.scalars(select(BarGap).where(BarGap.bar_set_id == set_id))
        }
    # Weekend: Friday 17:00 EST close to Sunday 18:00 EDT open, entirely closed.
    assert gaps[(ns("2024-03-08 22:00"), ns("2024-03-10 22:00"))] is False
    # Daily break 21:00-22:00 UTC (EDT), closed.
    assert gaps[(ns("2024-03-11 21:00"), ns("2024-03-11 22:00"))] is False
    # March to October: months of missing data while the market was open.
    assert gaps[(ns("2024-03-13 00:00"), ns("2024-10-30 00:00"))] is True


def test_rebuild_is_bit_identical(cfg: AppConfig, engine: Engine) -> None:
    build_clean(cfg, engine, "mt5_primary")
    version = build_bar_sets(cfg, engine, "mt5_primary").build_version
    root = cfg.paths.resolve(cfg.paths.data_dir) / "bars"
    before = {p: sha256_file(p) for p in root.rglob("*.parquet")}
    with session_factory(engine)() as session:
        set_hashes = {s.bar_set_id: s.sha256 for s in session.scalars(select(BarSet))}

    build_clean(cfg, engine, "mt5_primary", force=True)
    assert build_bar_sets(cfg, engine, "mt5_primary").build_version == version
    assert {p: sha256_file(p) for p in root.rglob("*.parquet")} == before
    with session_factory(engine)() as session:
        assert {s.bar_set_id: s.sha256 for s in session.scalars(select(BarSet))} == set_hashes


def test_cli_build_bars(tmp_path: Path) -> None:
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
    missing = runner.invoke(app, [*common, "build-bars", "--source", "mt5_primary"])
    assert missing.exit_code == 2
    assert "run `xq clean` first" in missing.stderr
    for command in (["ingest", "--path", str(FIXTURE_DIR)], ["clean"], ["build-bars"]):
        result = runner.invoke(app, [*common, command[0], "--source", "mt5_primary", *command[1:]])
        assert result.exit_code == 0, result.output
    assert "bars per timeframe: 1m " in result.stdout
    assert "1d 12" in result.stdout
