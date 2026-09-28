"""DATA-013: any hour of .bi5 records reads back at hour start + offset and points / scale."""

from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.dukascopy_fixtures import encode_bi5
from xq.core.config import load_config
from xq.data.adapters import DukascopyTickAdapter, RawFileRef, build_adapter
from xq.data.adapters.dukascopy import BI5_RECORD, HOUR_MS, bi5_file_name

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
ADAPTER = build_adapter(load_config("research", config_dir=REPO_CONFIG), "dukascopy")
HOURS = st.integers(min_value=289_000, max_value=500_000).map(  # 2002-12 ... 2027-01
    lambda h: pd.Timestamp(h * 3_600 * 10**9, unit="ns", tz="UTC")
)
RECORDS = st.lists(
    st.tuples(
        st.integers(0, HOUR_MS - 1),
        st.integers(1, 10_000_000),
        st.integers(1, 10_000_000),
        st.floats(0, 1_000, width=32),
        st.floats(0, 1_000, width=32),
    ),
    max_size=50,
)


@settings(max_examples=60, deadline=None)
@given(HOURS, RECORDS)
def test_bi5_records_read_back_exactly(hour: pd.Timestamp, rows: list[tuple]) -> None:
    assert isinstance(ADAPTER, DukascopyTickAdapter)
    records = np.array(rows, dtype=BI5_RECORD)
    name = bi5_file_name("XAUUSD", hour)
    with TemporaryDirectory() as directory:
        path = Path(directory) / name
        path.write_bytes(encode_bi5(records))
        raw = ADAPTER.read(RawFileRef(path, name, path.stat().st_size))
    ticks = ADAPTER.to_canonical(raw)
    order = np.argsort(records["ms"].astype(np.int64), kind="stable")
    expected_ns = hour.value + records["ms"].astype(np.int64)[order] * 1_000_000
    np.testing.assert_array_equal(ticks["ts_utc"].to_numpy(), expected_ns)
    np.testing.assert_array_equal(ticks["row_num"].to_numpy(), order)
    np.testing.assert_array_equal(ticks["ask"].to_numpy(), records["ask"][order] / 1000)
    np.testing.assert_array_equal(ticks["bid"].to_numpy(), records["bid"][order] / 1000)
    np.testing.assert_array_equal(raw["ask_volume"].to_numpy(), records["ask_volume"])
