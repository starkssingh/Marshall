"""DS-003 property: every causal primitive is truncation-invariant.

f(series up to row k) equals f(full series) on rows up to k, exactly, for random series (with
missing values and irregular spacing) and random k.
"""

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.primitive_cases import PRIMITIVE_CASES, SeriesFn, random_series
from xq.datasets.primitives import resample_causal


@pytest.mark.parametrize("name", sorted(PRIMITIVE_CASES))
@settings(max_examples=40, deadline=None)
@given(seed=st.integers(0, 2**31 - 1), cut=st.integers(1, 199))
def test_truncation_invariance(name: str, seed: int, cut: int) -> None:
    fn: SeriesFn = PRIMITIVE_CASES[name]
    series = random_series(seed)
    full = fn(series)
    truncated = fn(series.iloc[:cut])
    assert truncated.index.equals(series.index[:cut])
    np.testing.assert_array_equal(truncated.to_numpy(), full.iloc[:cut].to_numpy())


@pytest.mark.parametrize("how", ["last", "first", "sum", "mean", "max", "min"])
@settings(max_examples=40, deadline=None)
@given(seed=st.integers(0, 2**31 - 1), cut=st.integers(1, 199))
def test_resample_is_invariant_for_bins_known_at_the_cut(how: str, seed: int, cut: int) -> None:
    series = random_series(seed)
    cut_time = series.index[cut - 1]
    full = resample_causal(series, "15min", how, latency=pd.Timedelta(0))  # type: ignore[arg-type]
    truncated = resample_causal(series.iloc[:cut], "15min", how, latency=pd.Timedelta(0))  # type: ignore[arg-type]
    known: pd.Series = full[full.index <= cut_time]
    np.testing.assert_array_equal(
        truncated[truncated.index <= cut_time].to_numpy(), known.to_numpy()
    )
    assert truncated[truncated.index <= cut_time].index.equals(known.index)


@pytest.mark.parametrize("how", ["last", "first", "sum", "mean", "max", "min"])
@settings(max_examples=60, deadline=None)
@given(
    seed=st.integers(0, 2**31 - 1),
    latency_s=st.integers(1, 1800),
    decision_s=st.integers(0, 200 * 600),
)
def test_resample_with_latency_uses_only_available_rows(
    how: str, seed: int, latency_s: int, decision_s: int
) -> None:
    """Rows become available `latency` after their index time. At any decision time d, the bins
    labelled at or before d are the same whether computed from all rows or only from the rows
    available at d."""
    series = random_series(seed)
    latency = pd.Timedelta(seconds=latency_s)
    decision = series.index[0] + pd.Timedelta(seconds=decision_s)
    available = series[series.index + latency <= decision]
    full = resample_causal(series, "15min", how, latency=latency)  # type: ignore[arg-type]
    known = full[full.index <= decision]
    seen = resample_causal(available, "15min", how, latency=latency)  # type: ignore[arg-type]
    seen = seen[seen.index <= decision]
    assert seen.index.equals(known.index)
    np.testing.assert_array_equal(seen.to_numpy(), known.to_numpy())


def test_labelling_by_bin_end_alone_would_leak_with_latency() -> None:
    """The case the latency argument fixes: labelled by its end, a bin uses an unavailable row."""
    index = pd.DatetimeIndex(["2024-03-11 10:00", "2024-03-11 10:14"], tz="UTC")
    series = pd.Series([1.0, 2.0], index=index)
    latency = pd.Timedelta(minutes=2)
    decision = pd.Timestamp("2024-03-11 10:15", tz="UTC")  # the 10:14 row is known at 10:16
    naive = resample_causal(series, "15min", "last", latency=pd.Timedelta(0))
    assert naive.loc[decision] == 2.0  # would be read at 10:15, one minute too early
    fixed = resample_causal(series, "15min", "last", latency=latency)
    assert (fixed.index > decision).all()
