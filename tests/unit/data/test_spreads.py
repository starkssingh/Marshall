"""DATA-009: exact hour-of-week spread percentiles."""

import numpy as np
import pandas as pd
import pytest

from helpers.ticks import dense_ticks, widen_rollover
from xq.data.spreads import SpreadHistogram, hour_of_week


def ns(text: str) -> int:
    return int(pd.Timestamp(text, tz="UTC").value)


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        ("2024-03-11 04:00", 0),  # Monday 00:00 EDT
        ("2024-03-11 21:00", 17),  # Monday 17:00 EDT rollover
        ("2024-11-04 22:00", 17),  # Monday 17:00 EST rollover, same bucket after DST ends
        ("2024-03-10 22:00", 6 * 24 + 18),  # Sunday 18:00 EDT open
        ("2024-03-08 21:59", 4 * 24 + 16),  # Friday 16:59 EST
    ],
)
def test_hour_of_week_is_new_york_local(utc: str, expected: int) -> None:
    assert hour_of_week(np.array([ns(utc)])).tolist() == [expected]


def test_percentiles_are_exact_inverted_cdf() -> None:
    rng = np.random.default_rng(4)
    spread = np.round(rng.choice([0.15, 0.2, 0.25, 0.3, 0.5, 1.2], 5001), 2)
    ts = np.full(len(spread), ns("2024-03-11 14:30"))  # all in one hour of week
    histogram = SpreadHistogram(tick_size=0.01)
    histogram.add(ts[:2000], spread[:2000])  # adding in pieces must not matter
    histogram.add(ts[2000:], spread[2000:])
    (row,) = histogram.table().to_dict("records")
    assert row["n"] == 5001
    for name, q in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99)):
        assert row[name] == pytest.approx(np.percentile(spread, q * 100, method="inverted_cdf"))


def test_negative_spreads_are_refused() -> None:
    with pytest.raises(ValueError, match="crossed"):
        SpreadHistogram(0.01).add(np.array([ns("2024-03-11 14:30")]), np.array([-0.1]))


def test_rollover_widening_is_visible_in_the_profile() -> None:
    week = widen_rollover(
        dense_ticks("2024-03-11 00:00", "2024-03-16 00:00", seed=5, mean_interval_s=10)
    )
    histogram = SpreadHistogram(0.01)
    histogram.add(week["ts_utc"].to_numpy(), (week["ask"] - week["bid"]).to_numpy())
    table = histogram.table().set_index("hour_of_week")
    typical_p90 = table["p90"].median()
    for day in range(4):  # Monday..Thursday: 16:xx before the break, 18:xx after it
        assert table.loc[day * 24 + 16, "p90"] > 3 * typical_p90
        assert table.loc[day * 24 + 18, "p90"] > 3 * typical_p90
    assert table.loc[12, "p90"] < 1.5 * typical_p90  # midday Monday is ordinary
    assert 17 not in table.index  # the 17:00-18:00 break has no ticks
