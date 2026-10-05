"""Fixtures for the leakage suite (DS-006).

`bar_inputs` is a synthetic week of 15-minute and 1-hour bars built by the real bar builder from
dense synthetic ticks, with tz-aware ``bar_start_utc`` / ``available_at_utc`` like the catalog
returns. `assert_causal` runs the feature harness and fails with its report.
"""

from collections.abc import Callable

import pandas as pd
import pytest

from helpers.features import tick_bars
from helpers.ticks import dense_ticks
from xq.core.types import Timeframe
from xq.datasets.leakage import FeatureFn, Inputs, check_feature_causality

WEEK = ("2024-03-11 00:00", "2024-03-16 00:00")
AssertCausal = Callable[[FeatureFn, Inputs], None]


@pytest.fixture(scope="session")
def week_ticks() -> pd.DataFrame:
    """Dense synthetic canonical ticks (one about every 30 s) for a trading week."""
    return dense_ticks(*WEEK, seed=31, mean_interval_s=30)


@pytest.fixture(scope="session")
def bar_inputs(week_ticks: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Base (15m) and context (1h) bars of the synthetic week."""
    return {"base": tick_bars(week_ticks, Timeframe.M15), "h1": tick_bars(week_ticks, Timeframe.H1)}


#: Eight trading weeks: enough daily bars for the 1d context features' windows (FEAT-008).
LONG = ("2024-01-07 23:00", "2024-03-02 00:00")


@pytest.fixture(scope="session")
def feature_inputs() -> dict[str, pd.DataFrame]:
    """15m base bars and 1h, 4h and 1d context bars of eight synthetic trading weeks, keyed as the
    dataset builder keys a feature set's inputs (``base``, ``1h``, ``4h``, ``1d``)."""
    ticks = dense_ticks(*LONG, seed=37, mean_interval_s=90)
    return {
        "base": tick_bars(ticks, Timeframe.M15),
        "1h": tick_bars(ticks, Timeframe.H1),
        "4h": tick_bars(ticks, Timeframe.H4),
        "1d": tick_bars(ticks, Timeframe.D1),
    }


@pytest.fixture
def assert_causal() -> AssertCausal:
    """``assert_causal(fn, inputs)``: fail with the harness report if `fn` leaks."""

    def check(fn: FeatureFn, inputs: Inputs) -> None:
        report = check_feature_causality(fn, inputs, n_points=25, seed=7)
        assert report.passed, report.summary()

    return check
