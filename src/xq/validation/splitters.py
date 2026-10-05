"""Time-series splitters with purging and embargo (WF-001).

Samples are decision times (sorted, tz-aware) with the ``label_end`` of their target: the last
instant whose data the label depends on. A sample without a label (``label_end`` missing) is never
used for training or validation; it may still be predicted in a test window.

**Walk-forward** (`WalkForwardSplitter`): test windows follow each other from ``min_train +
val_len`` after the first sample, on the **retraining schedule** (WF-004): the model is refitted at
the start of each test window and predicts until the next.

- ``monthly`` (the plan's research default, used when no `test_len` is given): a window per
  calendar month, from the start of the trading day dated the 1st (17:00 New York the evening
  before, so it follows the trading-day roll and DST) to the next month's;
- ``fixed``: windows ``[test_start, test_start + test_len)`` every `step` (default `test_len`;
  never overlapping).

Windows abut unless a fixed `step` exceeds `test_len` (`WalkForwardConfig.contiguous`), so the
stitched out-of-sample series has neither overlaps nor gaps (`stitch_oos` in the runner checks it).
Each fold's validation window is the `val_len` before its test window and its
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

**Sample weights that read other labels** (C-30 (4), ADR 0065). Every splitter's `split` takes an
optional ``weight_end``: when sample weights (TGT-006's average uniqueness) are computed over
labels that reach into a later window, a sample's weight is known only at its ``weight_end``, so
every purge, embargo and cutoff above uses ``max(label_end, weight_end)`` instead of
``label_end``.

Shuffled splits do not exist here: financial samples are serially dependent (CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import BaseModel, ConfigDict, model_validator

from xq.core.errors import NaiveTimestampError
from xq.core.time import trading_day, trading_day_bounds
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
    #: The retraining schedule (module docstring): ``monthly`` unless a `test_len` is given.
    schedule: Literal["fixed", "monthly"] = "monthly"
    test_len: Duration | None = None
    step: Duration | None = None
    embargo: Duration = timedelta(0)
    purge_by: Literal["label_end"] = "label_end"

    @model_validator(mode="before")
    @classmethod
    def _default_schedule(cls, data: Any) -> Any:
        if isinstance(data, dict) and "schedule" not in data:
            data = {**data, "schedule": "fixed" if data.get("test_len") else "monthly"}
        return data

    @model_validator(mode="after")
    def _check(self) -> WalkForwardConfig:
        zero = pd.Timedelta(0)
        if pd.Timedelta(self.min_train) <= zero:
            raise ValueError("min_train must be positive")
        if pd.Timedelta(self.val_len) < zero or pd.Timedelta(self.embargo) < zero:
            raise ValueError("val_len and embargo must not be negative")
        if self.schedule == "monthly":
            if self.test_len is not None or self.step is not None:
                raise ValueError("a monthly schedule takes no test_len or step: the month is both")
            return self
        if self.test_len is None or pd.Timedelta(self.test_len) <= zero:
            raise ValueError("a fixed schedule needs a positive test_len")
        if self.step is not None and pd.Timedelta(self.step) < pd.Timedelta(self.test_len):
            raise ValueError("step must be at least test_len: test windows may not overlap")
        return self

    @property
    def contiguous(self) -> bool:
        """Whether consecutive test windows abut (no sample between them goes unpredicted)."""
        return self.schedule == "monthly" or self.step is None or self.step == self.test_len


class WalkForwardSplitter:
    """Walk-forward folds with purging and embargo (see the module docstring)."""

    def __init__(self, config: WalkForwardConfig) -> None:
        self.config = config
        self.min_train = pd.Timedelta(config.min_train).value
        self.val_len = pd.Timedelta(config.val_len).value
        self.embargo = pd.Timedelta(config.embargo).value

    def test_windows(self, first: int, last: int) -> list[tuple[int, int]]:
        """The test windows ``[start, end)`` (UTC nanoseconds) of samples from `first` to `last`.

        Windows start ``min_train + val_len`` after `first` (the first month boundary at or after
        it on a monthly schedule) and continue while they start at or before `last`.
        """
        start = first + self.min_train + self.val_len
        windows = []
        if self.config.schedule == "monthly":
            boundary = month_start(start, after=True)
            while boundary <= last:
                following = month_start(boundary + 1, after=True)
                windows.append((boundary, following))
                boundary = following
            return windows
        if self.config.test_len is None:  # refused by WalkForwardConfig for a fixed schedule
            raise ValueError("a fixed schedule needs a test_len")
        test_len = pd.Timedelta(self.config.test_len).value
        step = pd.Timedelta(self.config.step or self.config.test_len).value
        while start <= last:
            windows.append((start, start + test_len))
            start += step
        return windows

    def split(
        self,
        times: pd.DatetimeIndex,
        label_end: pd.Series | pd.DatetimeIndex,
        *,
        weight_end: pd.Series | pd.DatetimeIndex | None = None,
    ) -> list[Fold]:
        """All folds with at least one training and one test sample, in time order.

        Args:
            weight_end: When sample weights read other labels, when each sample's weight is known;
                samples are then purged by ``max(label_end, weight_end)`` (module docstring).

        Raises:
            NaiveTimestampError: if `times`, `label_end` or `weight_end` is not tz-aware.
            ValueError: if the inputs are misaligned, unsorted, or a label ends before its time.
        """
        t, e = _samples(times, label_end, weight_end)
        if len(t) == 0:
            return []
        labelled = e != _NAT
        folds: list[Fold] = []
        for test_start, test_end in self.test_windows(int(t[0]), int(t[-1])):
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
        return folds


def month_start(instant: int, *, after: bool) -> int:
    """The start of the trading day dated the 1st of a month (UTC nanoseconds): the latest at or
    before `instant`, or with `after` the earliest at or after it."""
    ts = pd.Timestamp(instant, tz="UTC")
    day = trading_day(ts)
    first = date(day.year, day.month, 1)
    boundary = int(trading_day_bounds(first)[0].value)
    if after and boundary < instant:
        following = date(day.year + day.month // 12, day.month % 12 + 1, 1)
        boundary = int(trading_day_bounds(following)[0].value)
    return boundary


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
        self,
        times: pd.DatetimeIndex,
        label_end: pd.Series | pd.DatetimeIndex,
        *,
        weight_end: pd.Series | pd.DatetimeIndex | None = None,
    ) -> list[Split]:
        """One split per group, in time order (see the module docstring)."""
        t, e = _samples(times, label_end, weight_end)
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
        self,
        times: pd.DatetimeIndex,
        label_end: pd.Series | pd.DatetimeIndex,
        *,
        weight_end: pd.Series | pd.DatetimeIndex | None = None,
    ) -> list[Split]:
        """``C(n_groups, k_test)`` splits in lexicographic order of their test groups."""
        t, e = _samples(times, label_end, weight_end)
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
    times: pd.DatetimeIndex,
    label_end: pd.Series | pd.DatetimeIndex,
    weight_end: pd.Series | pd.DatetimeIndex | None = None,
) -> tuple[IntArray, IntArray]:
    """Decision times and purge ends as int64 UTC nanoseconds (`_NAT` where no label): each
    ``label_end``, or ``max(label_end, weight_end)`` when weights are given."""
    index = pd.DatetimeIndex(times)
    ends = pd.DatetimeIndex(label_end)
    if index.tz is None or ends.tz is None:
        raise NaiveTimestampError("decision times and label_end must be tz-aware")
    if len(index) != len(ends):
        raise ValueError(f"{len(index)} decision times but {len(ends)} label ends")
    t = _ns(index)
    e = _ns(ends)
    if weight_end is not None:
        weights = pd.DatetimeIndex(weight_end)
        if weights.tz is None:
            raise NaiveTimestampError("weight_end must be tz-aware")
        if len(weights) != len(ends):
            raise ValueError(f"{len(ends)} label ends but {len(weights)} weight ends")
        w = _ns(weights)
        e = np.where(e == _NAT, _NAT, np.maximum(e, w))
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
