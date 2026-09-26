"""Run the data pipeline (ingest, clean, bars, spread statistics) in a temporary root."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from xq.core.config import AppConfig, load_config
from xq.data.bars import build_bar_sets
from xq.data.clean import build_clean
from xq.data.raw_store import ingest
from xq.data.spreads import build_spread_stats
from xq.tracking.db import create_db_engine, upgrade_to_head

REPO = Path(__file__).resolve().parents[2]


def config(root: Path, **overrides: Any) -> AppConfig:
    return load_config(
        "research",
        {"paths.root": str(root), "logging.file": None, "logging.console": False, **overrides},
        config_dir=REPO / "config",
    )


def run_pipeline(
    cfg: AppConfig, *source_dirs: Path, bars: bool = True, spreads: bool = True
) -> Engine:
    """Ingest `source_dirs` as mt5_primary, then clean and optionally build bars and spreads."""
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    for number, directory in enumerate(source_dirs):
        ingest(
            cfg,
            "mt5_primary",
            directory,
            engine=engine,
            run_id=f"01RUN{number:021d}",
            git_sha="test",
        )
    build_clean(cfg, engine, "mt5_primary")
    if bars:
        build_bar_sets(cfg, engine, "mt5_primary")
    if spreads:
        build_spread_stats(cfg, engine, "mt5_primary")
    return engine
