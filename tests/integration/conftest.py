"""Shared integration fixtures: a dense synthetic week written as an MT5 export."""

from pathlib import Path

import pytest

from helpers.ticks import dense_ticks, widen_rollover, write_mt5

DENSE_WEEK = ("2024-03-11 00:00", "2024-03-16 00:00")  # Monday to Friday close, after US DST


@pytest.fixture(scope="session")
def dense_week_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory with one MT5 export: a week of 10-second ticks with rollover widening."""
    directory = tmp_path_factory.mktemp("dense_week")
    week = widen_rollover(dense_ticks(*DENSE_WEEK, seed=21, mean_interval_s=10))
    write_mt5(week, directory / "XAUUSD_dense_week.csv")
    return directory
