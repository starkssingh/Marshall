"""Fixtures for the leakage suite (DS-006).

`bar_inputs` is a synthetic week of 15-minute and 1-hour bars built by the real bar builder from
dense synthetic ticks, with tz-aware ``bar_start_utc`` / ``available_at_utc`` like the catalog
returns. `assert_causal` runs the feature harness and fails with its report.
"""

from collections.abc import Callable

import pandas as pd
import pytest

from helpers.ticks import dense_ticks
from xq.core.types import Timeframe
from xq.data.bars import build_bars
from xq.datasets.leakage import FeatureFn, Inputs, check_feature_causality

WEEK = ("2024-03-11 00:00", "2024-03-16 00:00")
AssertCausal = Callable[[FeatureFn, Inputs], None]


def _bars(ticks: pd.DataFrame, tf: Timeframe) -> pd.DataFrame:
    bars = build_bars(
        ticks,
        tf,
        exclude_flags=0,
        latency_ns=0,
        coverage_end_ns=int(ticks["ts_utc"].max()) + 1,
    )
    frame = pd.DataFrame(
        {
            "bar_start_utc": pd.to_datetime(bars["bar_start_utc"], unit="ns", utc=True),
            "available_at_utc": pd.to_datetime(bars["available_at_utc"], unit="ns", utc=True),
            "open": bars["mid_open"],
            "high": bars["mid_high"],
            "low": bars["mid_low"],
            "close": bars["mid_close"],
            "tick_count": bars["tick_count"],
        }
    )
    return frame[bars["is_complete"].to_numpy()].reset_index(drop=True)


@pytest.fixture(scope="session")
def week_ticks() -> pd.DataFrame:
    """Dense synthetic canonical ticks (one about every 30 s) for a trading week."""
    return dense_ticks(*WEEK, seed=31, mean_interval_s=30)


@pytest.fixture(scope="session")
def bar_inputs(week_ticks: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Base (15m) and context (1h) bars of the synthetic week."""
    return {"base": _bars(week_ticks, Timeframe.M15), "h1": _bars(week_ticks, Timeframe.H1)}


@pytest.fixture
def assert_causal() -> AssertCausal:
    """``assert_causal(fn, inputs)``: fail with the harness report if `fn` leaks."""

    def check(fn: FeatureFn, inputs: Inputs) -> None:
        report = check_feature_causality(fn, inputs, n_points=25, seed=7)
        assert report.passed, report.summary()

    return check
