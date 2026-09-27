"""Time-series splitters with purging and embargo (WF-001).

Samples are decision times (sorted, tz-aware) with the ``label_end`` of their target: the last
instant whose data the label depends on. A sample without a label (``label_end`` missing) is never
used for training or validation; it may still be predicted in a test window.

**Walk-forward** (`WalkForwardSplitter`): test windows ``[test_start, test_start + test_len)``
follow each other every `step` (never overlapping), starting ``min_train + val_len`` after the
first sample. Each fold's validation window is the `val_len` before its test window and its
training window everything before that (``expanding``) or the `min_train` before that
(``rolling``). Purging by ``label_end`` keeps only training labels that end before
``validation start - embargo`` and validation labels that end before ``test start - embargo``, so
no label used for fitting or selection reaches into the next window. ``train_end`` is the latest
``label_end`` among a fold's training and validation samples — the information cutoff that
predictions are checked against (WF-003).

**Purged k-fold** (`PurgedKFold`, for inner cross-validation inside a training window) and
**combinatorial purged CV** (`CombinatorialPurgedCV`, for PBO) split samples into contiguous
groups. A training sample is dropped when its label interval ``[t, label_end]`` meets a test
group's span ``[first test time, last test label_end + embargo]``: purging removes labels that
overlap the test group, the embargo removes the samples right after it.

Shuffled splits do not exist here: financial samples are serially dependent (CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from itertools import combinations
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import BaseModel, ConfigDict, model_validator

from xq.core.errors import NaiveTimestampError
from xq.datasets.spec import Duration

IntArray = npt.NDArray[np.int64]
_NAT = np.iinfo(np.int64).min
_NO_EMBARGO = pd.Timedelta(0)


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold: positional indices into the samples and the window bounds (UTC)."""

    fold_id: str
    train_idx: IntArray
    val_idx: IntArray
    test_idx: IntArray
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


@dataclass(frozen=True)
class Split:
    """One purged k-fold or combinatorial split: indices and the test groups it holds out."""

    split_id: str
    train_idx: IntArray
    test_idx: IntArray
    test_groups: tuple[int, ...]


class WalkForwardConfig(BaseModel):
    """Walk-forward splitter parameters (durations accept ``"365D"``-style strings)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["expanding", "rolling"] = "expanding"
    min_train: Duration
    val_len: Duration = timedelta(0)
    test_len: Duration
    step: Duration | None = None
    embargo: Duration = timedelta(0)
    purge_by: Literal["label_end"] = "label_end"

    @model_validator(mode="after")
    def _check(self) -> WalkForwardConfig:
        zero = pd.Timedelta(0)
        if pd.Timedelta(self.min_train) <= zero or pd.Timedelta(self.test_len) <= zero:
            raise ValueError("min_train and test_len must be positive")
        if pd.Timedelta(self.val_len) < zero or pd.Timedelta(self.embargo) < zero:
            raise ValueError("val_len and embargo must not be negative")
        if self.step is not None and pd.Timedelta(self.step) < pd.Timedelta(self.test_len):
            raise ValueError("step must be at least test_len: test windows may not overlap")
        return self


class WalkForwardSplitter:
    """Walk-forward folds with purging and embargo (see the module docstring)."""

    def __init__(self, config: WalkForwardConfig) -> None:
        self.config = config
        self.min_train = pd.Timedelta(config.min_train).value
        self.val_len = pd.Timedelta(config.val_len).value
        self.test_len = pd.Timedelta(config.test_len).value
        self.step = pd.Timedelta(config.step or config.test_len).value
        self.embargo = pd.Timedelta(config.embargo).value

    def split(self, times: pd.DatetimeIndex, label_end: pd.Series | pd.DatetimeIndex) -> list[Fold]:
        """All folds with at least one training and one test sample, in time order.

        Raises:
            NaiveTimestampError: if `times` or `label_end` is not tz-aware.
            ValueError: if the inputs are misaligned, unsorted, or a label ends before its time.
        """
        t, e = _samples(times, label_end)
        if len(t) == 0:
            return []
        labelled = e != _NAT
        folds: list[Fold] = []
        test_start = int(t[0]) + self.min_train + self.val_len
        while test_start <= t[-1]:
            test_end = test_start + self.test_len
            val_start = test_start - self.val_len
            train_start = (
                int(t[0]) if self.config.mode == "expanding" else val_start - self.min_train
            )
            test_idx = _between(t, test_start, test_end)
            train = (t >= train_start) & (t < val_start) & labelled & (e < val_start - self.embargo)
            val = (t >= val_start) & (t < test_start) & labelled & (e < test_start - self.embargo)
            train_idx, val_idx = np.flatnonzero(train), np.flatnonzero(val)
            if len(test_idx) and len(train_idx):
                used = np.concatenate([train_idx, val_idx])
                cutoff = int(e[used].max())
                if cutoff >= test_start - self.embargo:  # the purge above makes this impossible
                    raise AssertionError("a training label reaches into the embargoed test window")
                folds.append(
                    Fold(
                        fold_id=f"f{len(folds):03d}",
                        train_idx=train_idx.astype(np.int64),
                        val_idx=val_idx.astype(np.int64),
                        test_idx=test_idx,
                        train_end=_ts(cutoff),
                        test_start=_ts(test_start),
                        test_end=_ts(test_end),
                    )
                )
            test_start += self.step
        return folds


class PurgedKFold:
    """K contiguous test groups; training samples purged and embargoed around each."""

    def __init__(self, n_splits: int, embargo: pd.Timedelta = _NO_EMBARGO) -> None:
        if n_splits < 2:
            raise ValueError("n_splits must be at least 2")
        if embargo < pd.Timedelta(0):
            raise ValueError("embargo must not be negative")
        self.n_splits = n_splits
        self.embargo = embargo.value

    def split(
        self, times: pd.DatetimeIndex, label_end: pd.Series | pd.DatetimeIndex
    ) -> list[Split]:
        """One split per group, in time order (see the module docstring)."""
        t, e = _samples(times, label_end)
        groups = _groups(len(t), self.n_splits)
        return [
            _purged_split(f"k{g:02d}", t, e, groups, (g,), self.embargo)
            for g in range(self.n_splits)
        ]


class CombinatorialPurgedCV:
    """Every choice of `k_test` of `n_groups` contiguous groups as the test set, purged."""

    def __init__(self, n_groups: int, k_test: int, embargo: pd.Timedelta = _NO_EMBARGO) -> None:
        if not 0 < k_test < n_groups:
            raise ValueError("k_test must be between 1 and n_groups - 1")
        if embargo < pd.Timedelta(0):
            raise ValueError("embargo must not be negative")
        self.n_groups = n_groups
        self.k_test = k_test
        self.embargo = embargo.value

    def split(
        self, times: pd.DatetimeIndex, label_end: pd.Series | pd.DatetimeIndex
    ) -> list[Split]:
        """``C(n_groups, k_test)`` splits in lexicographic order of their test groups."""
        t, e = _samples(times, label_end)
        groups = _groups(len(t), self.n_groups)
        return [
            _purged_split(
                "c" + "-".join(f"{g:02d}" for g in chosen), t, e, groups, chosen, self.embargo
            )
            for chosen in combinations(range(self.n_groups), self.k_test)
        ]


def _purged_split(
    split_id: str,
    t: IntArray,
    e: IntArray,
    groups: list[IntArray],
    chosen: tuple[int, ...],
    embargo: int,
) -> Split:
    test_idx = np.concatenate([groups[g] for g in chosen]).astype(np.int64)
    keep = e != _NAT
    keep[test_idx] = False
    for g in chosen:
        members = groups[g]
        first = t[members[0]]
        labels = e[members]
        last = max(int(t[members[-1]]), int(labels[labels != _NAT].max(initial=_NAT)))
        keep &= ~((e >= first) & (t <= last + embargo))
    return Split(split_id, np.flatnonzero(keep).astype(np.int64), test_idx, chosen)


def _groups(n: int, n_groups: int) -> list[IntArray]:
    if n < n_groups:
        raise ValueError(f"{n} samples cannot form {n_groups} groups")
    return [g.astype(np.int64) for g in np.array_split(np.arange(n), n_groups)]


def _samples(
    times: pd.DatetimeIndex, label_end: pd.Series | pd.DatetimeIndex
) -> tuple[IntArray, IntArray]:
    """Decision times and label ends as int64 UTC nanoseconds (`_NAT` where no label)."""
    index = pd.DatetimeIndex(times)
    ends = pd.DatetimeIndex(label_end)
    if index.tz is None or ends.tz is None:
        raise NaiveTimestampError("decision times and label_end must be tz-aware")
    if len(index) != len(ends):
        raise ValueError(f"{len(index)} decision times but {len(ends)} label ends")
    t = _ns(index)
    e = _ns(ends)
    if len(t) > 1 and np.any(np.diff(t) <= 0):
        raise ValueError("decision times must be unique and increasing")
    labelled = e != _NAT
    if np.any(e[labelled] < t[labelled]):
        raise ValueError("a label ends before its decision time")
    return t, e


def _between(t: IntArray, start: int, end: int) -> IntArray:
    lo, hi = np.searchsorted(t, [start, end], side="left")
    return np.arange(lo, hi, dtype=np.int64)


def _ns(index: pd.DatetimeIndex) -> IntArray:
    values: IntArray = (
        index.tz_convert("UTC").as_unit("ns").to_numpy(dtype="datetime64[ns]").view(np.int64)
    )
    return values


def _ts(value: int) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


__all__ = [
    "CombinatorialPurgedCV",
    "Fold",
    "PurgedKFold",
    "Split",
    "WalkForwardConfig",
    "WalkForwardSplitter",
]
