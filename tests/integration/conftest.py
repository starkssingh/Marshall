"""Shared integration fixtures: a dense synthetic week written as an MT5 export or as Dukascopy
hourly files."""

from pathlib import Path

import pytest

from helpers.dukascopy_fixtures import canonical_to_quotes, write_bi5_hours
from helpers.ticks import dense_ticks, widen_rollover, write_mt5

DENSE_WEEK = ("2024-03-11 00:00", "2024-03-16 00:00")  # Monday to Friday close, after US DST


@pytest.fixture(scope="session")
def dense_week_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory with one MT5 export: a week of 10-second ticks with rollover widening."""
    directory = tmp_path_factory.mktemp("dense_week")
    week = widen_rollover(dense_ticks(*DENSE_WEEK, seed=21, mean_interval_s=10))
    write_mt5(week, directory / "XAUUSD_dense_week.csv")
    return directory


@pytest.fixture(scope="session")
def clean_week_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory with one MT5 export: a week of 6-second ticks and no defects."""
    directory = tmp_path_factory.mktemp("clean_week")
    write_mt5(dense_ticks(*DENSE_WEEK, seed=22, mean_interval_s=6), directory / "XAUUSD_week.csv")
    return directory


@pytest.fixture(scope="session")
def dukascopy_clean_week_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The clean week's ticks as Dukascopy hourly .bi5 files (UTC), in the downloader's layout."""
    directory = tmp_path_factory.mktemp("dukascopy_clean_week")
    write_bi5_hours(
        canonical_to_quotes(dense_ticks(*DENSE_WEEK, seed=22, mean_interval_s=6)), directory
    )
    return directory
