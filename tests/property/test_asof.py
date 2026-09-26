"""DS-002 property: an as-of join only ever returns rows available at the decision time."""

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from xq.datasets.asof import asof_join

MINUTE_NS = 60 * 1_000_000_000
BASE_NS = pd.Timestamp("2024-01-01", tz="UTC").value


@settings(max_examples=200, deadline=None)
@given(
    right_minutes=st.lists(st.integers(0, 600), min_size=1, max_size=40),
    left_minutes=st.lists(st.integers(-60, 700), min_size=1, max_size=40),
    tolerance=st.one_of(st.none(), st.integers(0, 120)),
)
def test_match_is_the_latest_available_row(
    right_minutes: list[int], left_minutes: list[int], tolerance: int | None
) -> None:
    right_ns = BASE_NS + np.array(right_minutes, dtype=np.int64) * MINUTE_NS
    left_ns = BASE_NS + np.array(left_minutes, dtype=np.int64) * MINUTE_NS
    right = pd.DataFrame(
        {
            "available_at": pd.to_datetime(right_ns, unit="ns", utc=True),
            "value": np.arange(len(right_ns), dtype=np.float64),
        }
    )
    left = pd.DataFrame({"decision_time": pd.to_datetime(left_ns, unit="ns", utc=True)})
    joined = asof_join(
        left,
        right,
        tolerance=None if tolerance is None else pd.Timedelta(minutes=tolerance),
    )

    for i, t in enumerate(left_ns):
        got_value = joined["value"].iloc[i]
        got_time = joined["available_at"].iloc[i]
        available = right_ns <= t
        latest = right_ns[available].max() if available.any() else None
        if latest is None or (tolerance is not None and t - latest > tolerance * MINUTE_NS):
            assert np.isnan(got_value)
            assert pd.isna(got_time)
            continue
        # Never a row from the future; always the latest available one (last among ties).
        assert got_time.value == latest <= t
        assert got_value == float(np.flatnonzero(right_ns == latest)[-1])
