"""WF-006: for random sample spacing, label horizons and splitter settings, no fold leaks.

Walk-forward: every label used for fitting or selection ends before ``test_start - embargo``;
windows never overlap; purging removes exactly the labels that would reach the next window, not
more. Purged k-fold and CPCV: no training label interval meets a test group's span plus embargo.
"""

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from xq.validation.splitters import (
    CombinatorialPurgedCV,
    PurgedKFold,
    WalkForwardConfig,
    WalkForwardSplitter,
)

MINUTE = pd.Timedelta(minutes=1)


@st.composite
def samples(draw: st.DrawFn) -> tuple[pd.DatetimeIndex, pd.Series]:
    """Irregular decision times with random label horizons and some missing labels."""
    n = draw(st.integers(20, 300))
    seed = draw(st.integers(0, 2**31 - 1))
    rng = np.random.default_rng(seed)
    gaps = rng.integers(1, 6 * 60, size=n)  # 1 minute to 6 hours
    times = pd.Timestamp("2024-01-01", tz="UTC") + pd.to_timedelta(np.cumsum(gaps), unit="min")
    horizon = pd.to_timedelta(rng.integers(0, 48 * 60, size=n), unit="min")
    ends = pd.Series(pd.DatetimeIndex(times) + horizon)
    ends[rng.random(n) < 0.1] = pd.NaT
    return pd.DatetimeIndex(times), ends


def minutes(draw: st.DrawFn, low: int, high: int) -> str:
    return f"{draw(st.integers(low, high))}min"


@st.composite
def walk_forward_configs(draw: st.DrawFn) -> WalkForwardConfig:
    test_len = draw(st.integers(60, 5 * 24 * 60))
    return WalkForwardConfig(
        mode=draw(st.sampled_from(["expanding", "rolling"])),
        min_train=minutes(draw, 60, 10 * 24 * 60),  # type: ignore[arg-type]
        val_len=minutes(draw, 0, 3 * 24 * 60),  # type: ignore[arg-type]
        test_len=f"{test_len}min",  # type: ignore[arg-type]
        step=f"{test_len + draw(st.integers(0, 2 * 24 * 60))}min",  # type: ignore[arg-type]
        embargo=minutes(draw, 0, 24 * 60),  # type: ignore[arg-type]
    )


def ns(values: pd.DatetimeIndex | pd.Series) -> np.ndarray:
    return pd.DatetimeIndex(values).as_unit("ns").to_numpy("datetime64[ns]").view("int64")


@settings(max_examples=300, deadline=None)
@given(data=samples(), config=walk_forward_configs())
def test_walk_forward_folds_never_leak(
    data: tuple[pd.DatetimeIndex, pd.Series], config: WalkForwardConfig
) -> None:
    times, ends = data
    t, e = ns(times), ns(ends)
    nat = np.iinfo(np.int64).min
    embargo = pd.Timedelta(config.embargo).value
    val_len = pd.Timedelta(config.val_len).value
    min_train = pd.Timedelta(config.min_train).value
    tested: list[int] = []
    previous_end = None
    for fold in WalkForwardSplitter(config).split(times, ends):
        start, end = fold.test_start.value, fold.test_end.value
        used = np.concatenate([fold.train_idx, fold.val_idx])
        assert len(fold.train_idx)
        assert len(fold.test_idx)
        # the guard: no label used for fitting or selection reaches the embargoed test window
        assert (e[used] != nat).all()
        assert e[used].max() < start - embargo
        assert fold.train_end.value == e[used].max()
        # windows: test is exactly its span, validation right before it, training before that
        assert set(fold.test_idx) == set(np.flatnonzero((t >= start) & (t < end)))
        val_start = start - val_len
        assert ((t[fold.val_idx] >= val_start) & (t[fold.val_idx] < start)).all()
        assert (t[fold.train_idx] < val_start).all()
        if config.mode == "rolling":
            assert (t[fold.train_idx] >= val_start - min_train).all()
        # purging is exact: every labelled sample in the window whose label ends in time is kept
        lower = t[0] if config.mode == "expanding" else val_start - min_train
        window = (t >= lower) & (t < val_start) & (e != nat)
        assert set(fold.train_idx) == set(np.flatnonzero(window & (e < val_start - embargo)))
        in_val = (t >= val_start) & (t < start) & (e != nat)
        assert set(fold.val_idx) == set(np.flatnonzero(in_val & (e < start - embargo)))
        # test windows move forward without overlapping
        if previous_end is not None:
            assert start >= previous_end
        previous_end = end
        tested.extend(fold.test_idx.tolist())
    assert len(tested) == len(set(tested))  # every sample is predicted at most once


@settings(max_examples=200, deadline=None)
@given(
    data=samples(),
    n_groups=st.integers(2, 8),
    k_test=st.integers(1, 3),
    embargo_min=st.integers(0, 12 * 60),
)
def test_purged_kfold_and_cpcv_never_train_on_a_test_span(
    data: tuple[pd.DatetimeIndex, pd.Series], n_groups: int, k_test: int, embargo_min: int
) -> None:
    times, ends = data
    t, e = ns(times), ns(ends)
    nat = np.iinfo(np.int64).min
    embargo = pd.Timedelta(minutes=embargo_min)
    groups = np.array_split(np.arange(len(t)), n_groups)
    splits = PurgedKFold(n_groups, embargo=embargo).split(times, ends)
    if k_test < n_groups:
        splits += CombinatorialPurgedCV(n_groups, k_test, embargo=embargo).split(times, ends)
    for split in splits:
        assert not set(split.train_idx) & set(split.test_idx)
        assert (e[split.train_idx] != nat).all()
        allowed = e != nat
        allowed[split.test_idx] = False
        for g in split.test_groups:
            members = groups[g]
            labels = e[members]
            last = max(t[members[-1]], labels[labels != nat].max(initial=nat))
            meets = (e >= t[members[0]]) & (t <= last + embargo.value)
            assert not meets[split.train_idx].any()  # no leak
            allowed &= ~meets
        assert set(split.train_idx) == set(np.flatnonzero(allowed))  # and no over-purging
