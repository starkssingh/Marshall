"""Build `PartitionData` for quality-check tests from canonical ticks."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from xq.core.config import AppConfig, load_config
from xq.core.time import to_ns
from xq.core.types import Timeframe
from xq.data.bars import build_bars, exclude_mask
from xq.data.clean import MarketWindow, clean_ticks
from xq.data.sessions import build_session_table
from xq.quality.registry import PartitionData

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def repo_config() -> AppConfig:
    return load_config("research", config_dir=REPO_CONFIG)


def day_row(cfg: AppConfig, day: date) -> pd.Series:
    return build_session_table(cfg.sessions_config(), day, day).iloc[0]


def market_window(row: pd.Series) -> MarketWindow:
    if not row["is_open"]:
        return MarketWindow(None, None)
    return MarketWindow(to_ns(row["market_open_utc"]), to_ns(row["market_close_utc"]))


def partition(
    ticks: pd.DataFrame,
    day: date,
    *,
    cfg: AppConfig | None = None,
    clean: bool = True,
    spread_stats: pd.DataFrame | None = None,
    hourly_tick_norm: pd.Series | None = None,
    bars_1m: pd.DataFrame | None = None,
    dropped: dict[str, int] | None = None,
) -> PartitionData:
    """A partition of `ticks` for trading day `day`, cleaned with the repository's rules."""
    cfg = cfg or repo_config()
    row = day_row(cfg, day)
    if clean:
        ticks = clean_ticks(ticks, cfg.cleaning_config(), market_window(row)).ticks
    ticks = ticks.sort_values("ts_utc", kind="stable", ignore_index=True)
    if bars_1m is None:
        bars_1m = build_bars(
            ticks,
            Timeframe.M1,
            exclude_flags=exclude_mask(cfg.bars_config()),
            latency_ns=0,
            coverage_end_ns=int(ticks["ts_utc"].max()) if len(ticks) else 0,
        )
    return PartitionData(
        source_id="mt5_primary",
        instrument_id="xauusd",
        trading_day=day,
        ticks=ticks,
        bars_1m=bars_1m,
        day=row,
        spread_stats=spread_stats,
        hourly_tick_norm=hourly_tick_norm,
        dropped=dropped or {},
    )
