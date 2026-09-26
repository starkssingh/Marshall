"""DATA-009: spread statistics persisted per source and hour of week, vault excluded."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

from xq.cli.main import app
from xq.core.config import AppConfig, load_config
from xq.data.clean import build_clean
from xq.data.raw_store import ingest
from xq.data.spreads import (
    NoSpreadDataError,
    SpreadHistogram,
    build_spread_stats,
    latest_spread_stats,
)
from xq.tracking.db import create_db_engine, upgrade_to_head

REPO = Path(__file__).resolve().parents[3]


def make_config(root: Path, **overrides: Any) -> AppConfig:
    return load_config(
        "research",
        {"paths.root": str(root), "logging.file": None, "logging.console": False, **overrides},
        config_dir=REPO / "config",
    )


def pipeline(cfg: AppConfig, source_dir: Path) -> Engine:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    ingest(
        cfg,
        "mt5_primary",
        source_dir,
        engine=engine,
        run_id="01RUNA0000000000000000000A",
        git_sha="test",
    )
    build_clean(cfg, engine, "mt5_primary")
    return engine


@pytest.fixture
def engine(tmp_path: Path, dense_week_dir: Path) -> Iterator[Engine]:
    engine = pipeline(make_config(tmp_path), dense_week_dir)
    yield engine
    engine.dispose()


def test_stored_statistics_match_the_clean_ticks(tmp_path: Path, engine: Engine) -> None:
    cfg = make_config(tmp_path)
    result = build_spread_stats(cfg, engine, "mt5_primary")
    stored = latest_spread_stats(engine, "mt5_primary").set_index("hour_of_week")

    clean = pd.read_parquet(cfg.paths.resolve(cfg.paths.data_dir) / "clean")
    expected = SpreadHistogram(0.01)
    expected.add(clean["ts_utc"].to_numpy(), (clean["ask"] - clean["bid"]).to_numpy())
    reference = expected.table().set_index("hour_of_week")
    pd.testing.assert_frame_equal(stored, reference, check_dtype=False)
    assert result.ticks == len(clean) == int(stored["n"].sum())
    assert result.computed_from == pd.Timestamp("2024-03-10 21:00", tz="UTC")
    assert result.computed_to == pd.Timestamp("2024-03-15 21:00", tz="UTC")


def test_rollover_spike_is_visible_end_to_end(tmp_path: Path, engine: Engine) -> None:
    build_spread_stats(make_config(tmp_path), engine, "mt5_primary")
    stats = latest_spread_stats(engine, "mt5_primary").set_index("hour_of_week")
    typical = stats["p90"].median()
    assert stats.loc[16, "p90"] > 3 * typical  # Monday 16:50-17:00 New York
    assert stats.loc[18, "p90"] > 3 * typical  # Monday 18:00-18:30 New York
    assert stats.loc[12, "p90"] < 1.5 * typical


def test_recomputing_the_same_window_replaces_rows(tmp_path: Path, engine: Engine) -> None:
    cfg = make_config(tmp_path)
    build_spread_stats(cfg, engine, "mt5_primary")
    first = latest_spread_stats(engine, "mt5_primary")
    build_spread_stats(cfg, engine, "mt5_primary")
    pd.testing.assert_frame_equal(latest_spread_stats(engine, "mt5_primary"), first)


def test_vault_data_is_left_out(tmp_path: Path, dense_week_dir: Path) -> None:
    cfg = make_config(tmp_path, **{"vault.start": "2024-03-13T12:00:00Z"})
    engine = pipeline(cfg, dense_week_dir)
    result = build_spread_stats(cfg, engine, "mt5_primary")
    assert result.computed_to == pd.Timestamp("2024-03-13 12:00", tz="UTC")
    clean = pd.read_parquet(cfg.paths.resolve(cfg.paths.data_dir) / "clean")
    before_vault = clean["ts_utc"] < pd.Timestamp("2024-03-13 12:00", tz="UTC").value
    assert result.ticks == int(before_vault.sum()) < len(clean)
    engine.dispose()

    late = make_config(tmp_path / "late", **{"vault.start": "2024-03-01T00:00:00Z"})
    late_engine = pipeline(late, dense_week_dir)
    with pytest.raises(NoSpreadDataError, match="before the vault"):
        build_spread_stats(late, late_engine, "mt5_primary")
    late_engine.dispose()


def test_cli_spread_stats(tmp_path: Path, dense_week_dir: Path) -> None:
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
    for command in (["ingest", "--path", str(dense_week_dir)], ["clean"], ["spread-stats"]):
        result = runner.invoke(app, [*common, command[0], "--source", "mt5_primary", *command[1:]])
        assert result.exit_code == 0, result.output
    hours = 4 + 4 * 23 + 17  # Sunday 20:00-24:00 New York, Mon-Thu, Friday until 17:00
    assert f"spread statistics for {hours} hour(s) of week" in result.stdout
