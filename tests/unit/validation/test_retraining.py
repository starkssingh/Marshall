"""WF-004: the retraining schedule (monthly by default in research, or a fixed interval) and the
stitched out-of-sample series, which has no overlaps or gaps."""

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from helpers.models import MEAN
from xq.models.base import ModelConfig
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter
from xq.validation.walkforward import StitchError, stitch_oos, walk_forward

H = pd.Timedelta(hours=1)


def hourly(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="h", tz="UTC", inclusive="left", name="decision_time")


def run(index: pd.DatetimeIndex, splitter: WalkForwardConfig) -> pd.DataFrame:
    y = pd.Series(np.random.default_rng(0).normal(size=len(index)), index=index)
    output = walk_forward(
        pd.DataFrame(index=index),
        y,
        pd.Series(index + 2 * H, index=index),
        MEAN,
        ModelConfig(name=MEAN.name),
        splitter,
        seed=3,
    )
    return output.predictions


def test_monthly_is_the_default_without_a_test_length() -> None:
    monthly = WalkForwardConfig.model_validate({"min_train": "60D"})
    assert monthly.schedule == "monthly"
    assert monthly.contiguous
    fixed = WalkForwardConfig.model_validate({"min_train": "60D", "test_len": "7D"})
    assert fixed.schedule == "fixed"
    assert fixed.contiguous
    sparse = WalkForwardConfig.model_validate({"min_train": "60D", "test_len": "7D", "step": "9D"})
    assert not sparse.contiguous
    with pytest.raises(ValidationError, match="takes no test_len or step"):
        WalkForwardConfig.model_validate(
            {"min_train": "60D", "schedule": "monthly", "test_len": "7D"}
        )
    with pytest.raises(ValidationError, match="needs a positive test_len"):
        WalkForwardConfig.model_validate({"min_train": "60D", "schedule": "fixed"})


def test_monthly_windows_follow_the_trading_day_roll_in_both_seasons() -> None:
    """Retraining at the start of the trading day dated the 1st: 17:00 New York the evening
    before, 22:00 UTC in winter (EST) and 21:00 UTC in summer (EDT)."""
    index = hourly("2024-01-01", "2024-07-15")
    folds = WalkForwardSplitter(WalkForwardConfig.model_validate({"min_train": "45D"})).split(
        index, pd.Series(index + 2 * H)
    )
    starts = [f.test_start for f in folds]
    assert starts == list(
        pd.to_datetime(
            [
                "2024-02-29T22:00Z",  # trading day 2024-03-01 (EST)
                "2024-03-31T21:00Z",  # trading day 2024-04-01 (EDT since 10 March)
                "2024-04-30T21:00Z",
                "2024-05-31T21:00Z",
                "2024-06-30T21:00Z",
            ],
            utc=True,
        )
    )
    assert [f.test_end for f in folds][:-1] == starts[1:]  # windows abut
    assert folds[-1].test_end == pd.Timestamp("2024-07-31T21:00Z")


def test_the_stitched_series_has_no_overlaps_or_gaps() -> None:
    index = hourly("2024-01-01", "2024-07-15")
    for splitter in (
        WalkForwardConfig.model_validate({"min_train": "45D", "embargo": "3h"}),
        WalkForwardConfig.model_validate({"min_train": "45D", "test_len": "10D"}),
        WalkForwardConfig.model_validate({"min_train": "45D", "mode": "rolling", "val_len": "5D"}),
    ):
        predictions = run(index, splitter)
        assert predictions.index.is_unique
        assert predictions.index.is_monotonic_increasing
        first = predictions.index[0]
        assert predictions.index.equals(index[index >= first])  # every later sample, once
        # each fold retrained before it predicts: its training data ends before its window
        for _, fold in predictions.groupby("fold_id"):
            assert (fold["train_end"] < fold.index.min()).all()


def test_a_fixed_step_longer_than_the_window_leaves_declared_gaps() -> None:
    index = hourly("2024-01-01", "2024-04-01")
    splitter = WalkForwardConfig.model_validate(
        {"min_train": "30D", "test_len": "5D", "step": "8D"}
    )
    predictions = run(index, splitter)
    assert predictions.index.is_unique
    assert len(predictions) < (index >= predictions.index[0]).sum()


def test_stitching_refuses_overlaps_gaps_and_strays() -> None:
    index = hourly("2024-01-01", "2024-01-05")
    day = pd.Timedelta(days=1)

    def frame(start: int, end: int) -> pd.DataFrame:
        return pd.DataFrame({"y_pred": 0.0}, index=index[start:end])

    windows = [(index[0], index[0] + day), (index[0] + day, index[0] + 2 * day)]
    stitched = stitch_oos([frame(0, 24), frame(24, 48)], windows, index, contiguous=True)
    assert stitched.index.equals(index[:48])
    with pytest.raises(StitchError, match="gap"):
        stitch_oos([frame(0, 24), frame(30, 48)], windows, index, contiguous=True)
    assert len(stitch_oos([frame(0, 24), frame(30, 48)], windows, index, contiguous=False)) == 42
    overlapping = [(index[0], index[0] + 2 * day), (index[0] + day, index[0] + 2 * day)]
    with pytest.raises(StitchError, match="overlap"):
        stitch_oos([frame(0, 48), frame(24, 48)], overlapping, index, contiguous=True)
    with pytest.raises(StitchError, match="outside its test window"):
        stitch_oos([frame(0, 30), frame(30, 48)], windows, index, contiguous=True)
    assert stitch_oos([], [], index, contiguous=True).empty
