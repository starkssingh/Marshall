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
    full = resample_causal(series, "15min", how)  # type: ignore[arg-type]
    truncated = resample_causal(series.iloc[:cut], "15min", how)  # type: ignore[arg-type]
    known: pd.Series = full[full.index <= cut_time]
    np.testing.assert_array_equal(
        truncated[truncated.index <= cut_time].to_numpy(), known.to_numpy()
    )
    assert truncated[truncated.index <= cut_time].index.equals(known.index)
