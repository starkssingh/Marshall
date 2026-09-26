"""DS-002: as-of joins on availability."""

import numpy as np
import pandas as pd
import pytest

from xq.core.errors import NaiveTimestampError
from xq.datasets.asof import asof_join

H = pd.Timedelta(hours=1)
M15 = pd.Timedelta(minutes=15)
T0 = pd.Timestamp("2024-03-12 09:00", tz="UTC")


def hourly_bars() -> pd.DataFrame:
    """Three 1h bars starting 09:00, 10:00, 11:00; each available at its end."""
    starts = pd.date_range(T0, periods=3, freq="1h")
    return pd.DataFrame(
        {
            "bar_start_utc": starts,
            "available_at_utc": starts + H,
            "close": [100.0, 101.0, 102.0],
            "tick_count": [10, 20, 30],
        }
    )


def decisions(times: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"decision_time": pd.to_datetime(times, utc=True), "x": range(len(times))})


def test_higher_timeframe_value_appears_only_after_its_available_at() -> None:
    left = decisions(
        ["2024-03-12 09:45", "2024-03-12 10:00", "2024-03-12 10:45", "2024-03-12 11:00"]
    )
    joined = asof_join(left, hourly_bars(), on_right="available_at_utc", prefix="h1_")
    # 09:45: the 09:00 bar closes at 10:00, so nothing is available yet.
    assert np.isnan(joined["h1_close"].iloc[0])
    assert pd.isna(joined["h1_available_at"].iloc[0])
    # 10:00 exactly: the 09:00 bar is available (available_at <= decision time).
    assert joined["h1_close"].tolist()[1:] == [100.0, 100.0, 101.0]
    assert (joined["h1_available_at"].iloc[1:] <= joined["decision_time"].iloc[1:]).all()
    # The bar start is joined as a value, never used as the key.
    assert joined["h1_bar_start_utc"].iloc[3] == T0 + H


def test_integer_columns_become_float_when_unmatched() -> None:
    joined = asof_join(
        decisions(["2024-03-12 09:30", "2024-03-12 10:30"]),
        hourly_bars(),
        on_right="available_at_utc",
        columns=["tick_count"],
    )
    assert joined.columns.tolist() == ["decision_time", "x", "tick_count", "available_at"]
    assert np.isnan(joined["tick_count"].iloc[0])
    assert joined["tick_count"].iloc[1] == 10


def test_tolerance_drops_stale_matches() -> None:
    left = decisions(["2024-03-12 12:10", "2024-03-12 13:30"])
    joined = asof_join(
        left, hourly_bars(), on_right="available_at_utc", tolerance="45min", columns=["close"]
    )
    assert joined["close"].iloc[0] == 102.0
    assert np.isnan(joined["close"].iloc[1])
    assert pd.isna(joined["available_at"].iloc[1])


def test_left_order_and_index_are_kept() -> None:
    left = decisions(["2024-03-12 11:30", "2024-03-12 10:30", "2024-03-12 12:30"])
    left.index = pd.Index([7, 3, 5])
    joined = asof_join(left, hourly_bars(), on_right="available_at_utc", columns=["close"])
    assert joined.index.tolist() == [7, 3, 5]
    assert joined["close"].tolist() == [101.0, 100.0, 102.0]


def test_unsorted_right_and_ties_take_the_last_row() -> None:
    right = hourly_bars().iloc[[2, 0, 1]]
    tie = pd.DataFrame(
        {
            "bar_start_utc": [T0 + H],
            "available_at_utc": [T0 + 2 * H],
            "close": [999.0],
            "tick_count": [1],
        }
    )
    right = pd.concat([right, tie], ignore_index=True)
    joined = asof_join(
        decisions(["2024-03-12 11:05"]), right, on_right="available_at_utc", columns=["close"]
    )
    assert joined["close"].iloc[0] == 999.0


def test_missing_decision_times_get_no_match() -> None:
    left = pd.DataFrame({"decision_time": pd.to_datetime([None, "2024-03-12 11:00"], utc=True)})
    joined = asof_join(left, hourly_bars(), on_right="available_at_utc", columns=["close"])
    assert np.isnan(joined["close"].iloc[0])
    assert joined["close"].iloc[1] == 101.0


def test_join_on_bar_start_is_refused() -> None:
    with pytest.raises(ValueError, match="not an available_at column"):
        asof_join(decisions(["2024-03-12 10:30"]), hourly_bars(), on_right="bar_start_utc")


def test_naive_keys_are_refused() -> None:
    left = pd.DataFrame({"decision_time": pd.to_datetime(["2024-03-12 10:30"])})
    with pytest.raises(NaiveTimestampError, match="decision_time"):
        asof_join(left, hourly_bars(), on_right="available_at_utc")
    right = hourly_bars()
    right["available_at_utc"] = right["available_at_utc"].dt.tz_localize(None)
    with pytest.raises(NaiveTimestampError, match="available_at_utc"):
        asof_join(decisions(["2024-03-12 10:30"]), right, on_right="available_at_utc")


def test_name_collisions_and_missing_availability_are_refused() -> None:
    left = decisions(["2024-03-12 10:30"]).assign(close=1.0)
    with pytest.raises(ValueError, match="overwrite"):
        asof_join(left, hourly_bars(), on_right="available_at_utc", columns=["close"])
    right = hourly_bars()
    right.loc[1, "available_at_utc"] = pd.NaT
    with pytest.raises(ValueError, match="missing availability"):
        asof_join(decisions(["2024-03-12 10:30"]), right, on_right="available_at_utc")


def test_non_utc_zones_are_compared_as_instants() -> None:
    left = pd.DataFrame(
        {"decision_time": pd.to_datetime(["2024-03-12 06:00"]).tz_localize("America/New_York")}
    )
    joined = asof_join(left, hourly_bars(), on_right="available_at_utc", columns=["close"])
    assert joined["close"].iloc[0] == 100.0  # 06:00 EDT = 10:00 UTC


def test_empty_right_matches_nothing() -> None:
    empty = hourly_bars().iloc[0:0]
    joined = asof_join(
        decisions(["2024-03-12 10:30", "2024-03-12 12:30"]),
        empty,
        on_right="available_at_utc",
        prefix="h1_",
    )
    assert joined["h1_close"].isna().all()
    assert joined["h1_tick_count"].isna().all()
    assert joined["h1_available_at"].isna().all()
    assert str(joined["h1_available_at"].dtype) == "datetime64[ns, UTC]"
