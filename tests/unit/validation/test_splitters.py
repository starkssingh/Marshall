"""WF-001: walk-forward, purged k-fold and combinatorial splitters on hand-computed cases."""

from math import comb

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from xq.core.errors import NaiveTimestampError
from xq.validation.splitters import (
    CombinatorialPurgedCV,
    PurgedKFold,
    WalkForwardConfig,
    WalkForwardSplitter,
)

H = pd.Timedelta(hours=1)
D = pd.Timedelta(days=1)
T = pd.date_range("2024-01-01", periods=240, freq="h", tz="UTC")  # ten days of hourly samples
ENDS = pd.Series(T + 2 * H)  # every label ends two hours after its decision


def splitter(**params: object) -> WalkForwardSplitter:
    return WalkForwardSplitter(
        WalkForwardConfig.model_validate({"min_train": "3D", "test_len": "1D", **params})
    )


def test_expanding_folds_purge_training_labels_before_the_embargoed_test_start() -> None:
    folds = splitter(embargo="1h").split(T, ENDS)
    assert [f.fold_id for f in folds] == [f"f{i:03d}" for i in range(7)]
    first = folds[0]
    assert first.test_start == T[0] + 3 * D
    assert first.test_end == T[0] + 4 * D
    np.testing.assert_array_equal(first.test_idx, np.arange(72, 96))
    # a label t + 2h must end before test_start - 1h, so the last three hours are purged
    np.testing.assert_array_equal(first.train_idx, np.arange(0, 69))
    assert first.train_end == first.test_start - 2 * H
    assert len(first.val_idx) == 0
    last = folds[-1]
    np.testing.assert_array_equal(last.train_idx, np.arange(0, 213))  # expanding
    np.testing.assert_array_equal(last.test_idx, np.arange(216, 240))


def test_rolling_windows_keep_their_length() -> None:
    folds = splitter(mode="rolling").split(T, ENDS)
    assert [len(f.train_idx) for f in folds] == [70] * 7  # 72 hours minus two purged
    assert folds[1].train_idx[0] == 24


def test_validation_window_is_purged_against_test_and_training_against_validation() -> None:
    folds = splitter(val_len="1D", embargo="1h").split(T, ENDS)
    first = folds[0]
    assert first.test_start == T[0] + 4 * D  # min_train + val_len after the first sample
    np.testing.assert_array_equal(first.val_idx, np.arange(72, 93))
    np.testing.assert_array_equal(first.train_idx, np.arange(0, 69))
    assert first.train_end == first.test_start - 2 * H  # the last validation label
    assert len(folds) == 6


def test_samples_without_a_label_are_predicted_but_never_trained_on() -> None:
    ends = ENDS.copy()
    ends.iloc[[10, 80]] = pd.NaT
    first = splitter().split(T, ends)[0]
    assert 10 not in first.train_idx
    assert 80 in first.test_idx


def test_step_longer_than_the_test_window_leaves_gaps_and_empty_windows_are_skipped() -> None:
    folds = splitter(step="2D").split(T, ENDS)
    assert [f.test_start for f in folds] == [T[0] + d * D for d in (3, 5, 7, 9)]
    day = (T - T[0]) // D
    sparse = T[(day < 4) | (day >= 6)]  # no samples on days 4 and 5
    starts = [f.test_start for f in splitter().split(sparse, pd.Series(sparse + 2 * H))]
    assert T[0] + 4 * D not in starts  # a window without samples yields no fold
    assert T[0] + 5 * D not in starts


@pytest.mark.parametrize(
    "params",
    [
        {"step": "12h"},  # overlapping test windows
        {"embargo": "-1h"},
        {"min_train": "0D"},
        {"test_len": "0D"},
        {"purge_by": "decision_time"},
    ],
)
def test_config_is_validated(params: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        WalkForwardConfig.model_validate({"min_train": "3D", "test_len": "1D", **params})


def test_inputs_are_validated() -> None:
    with pytest.raises(NaiveTimestampError):
        splitter().split(T.tz_localize(None), ENDS)
    with pytest.raises(ValueError, match="increasing"):
        splitter().split(T[::-1], ENDS[::-1])
    with pytest.raises(ValueError, match="before its decision"):
        splitter().split(T, pd.Series(T - H))
    with pytest.raises(ValueError, match="label ends"):
        splitter().split(T, ENDS.iloc[:-1])
    assert splitter().split(T[:0], ENDS.iloc[:0]) == []


def test_purged_kfold_removes_overlapping_labels_and_the_embargo() -> None:
    times = T[:100]
    ends = pd.Series(times + 3 * H)
    splits = PurgedKFold(5, embargo=2 * H).split(times, ends)
    assert [s.split_id for s in splits] == ["k00", "k01", "k02", "k03", "k04"]
    middle = splits[2]
    np.testing.assert_array_equal(middle.test_idx, np.arange(40, 60))
    # before: labels of samples 37..39 reach sample 40; after: the test labels end at sample 62
    # and the 2-hour embargo removes samples up to 64
    expected = np.concatenate([np.arange(0, 37), np.arange(65, 100)])
    np.testing.assert_array_equal(middle.train_idx, expected)
    np.testing.assert_array_equal(splits[0].train_idx, np.arange(25, 100))


def test_combinatorial_splits_hold_out_every_pair_of_groups() -> None:
    times = T[:120]
    ends = pd.Series(times + H)
    splits = CombinatorialPurgedCV(6, 2).split(times, ends)
    assert len(splits) == comb(6, 2)
    assert splits[0].split_id == "c00-01"
    assert splits[0].test_groups == (0, 1)
    counts = np.zeros(120, dtype=int)
    for s in splits:
        counts[s.test_idx] += 1
        assert not set(s.train_idx) & set(s.test_idx)
    assert (counts == comb(5, 1)).all()  # every sample is tested in n-1 choose k-1 splits
    split = next(s for s in splits if s.test_groups == (1, 3))
    np.testing.assert_array_equal(split.test_idx, np.concatenate([range(20, 40), range(60, 80)]))
    assert 19 not in split.train_idx  # its label reaches group 1
    assert 40 not in split.train_idx  # right after group 1: its span ends with sample 40's time
    assert 41 in split.train_idx


@pytest.mark.parametrize(
    ("make", "match"),
    [
        (lambda: PurgedKFold(1), "at least 2"),
        (lambda: CombinatorialPurgedCV(4, 4), "k_test"),
        (lambda: CombinatorialPurgedCV(4, 0), "k_test"),
    ],
)
def test_kfold_parameters_are_validated(make: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        make()  # type: ignore[operator]


def test_too_few_samples_for_the_groups() -> None:
    with pytest.raises(ValueError, match="cannot form"):
        PurgedKFold(5).split(T[:3], pd.Series(T[:3]))
