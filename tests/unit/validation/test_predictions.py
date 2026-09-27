"""WF-003: the prediction store refuses rows made at or before the training cutoff + embargo."""

import numpy as np
import pandas as pd
import pytest

from xq.core.errors import NaiveTimestampError
from xq.validation.predictions import PredictionLeakError, check_predictions

T = pd.date_range("2024-03-12 10:00", periods=4, freq="h", tz="UTC", name="decision_time")
H = pd.Timedelta(hours=1)


def frame(train_end: pd.Timestamp = T[0] - 2 * H, index: pd.DatetimeIndex = T) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "fold_id": "f000",
            "y_true": np.zeros(len(index)),
            "y_pred": np.zeros(len(index)),
            "p_raw": np.nan,
            "p_cal": np.nan,
            "train_end": train_end,
        },
        index=index,
    )


def test_rows_after_the_cutoff_plus_embargo_pass() -> None:
    check_predictions(frame(), embargo=H)


@pytest.mark.parametrize(
    ("train_end", "embargo"),
    [
        (T[0], pd.Timedelta(0)),  # a prediction at the cutoff itself
        (T[0] - H, H),  # inside the embargo
        (T[0] + H, pd.Timedelta(0)),  # trained on its own future
    ],
)
def test_leaked_rows_are_refused(train_end: pd.Timestamp, embargo: pd.Timedelta) -> None:
    with pytest.raises(PredictionLeakError, match="f000"):
        check_predictions(frame(train_end), embargo=embargo)


def test_missing_cutoff_is_refused() -> None:
    leaky = frame()
    leaky.loc[T[2], "train_end"] = pd.NaT
    with pytest.raises(PredictionLeakError, match="1 leaked"):
        check_predictions(leaky, embargo=H)


def test_malformed_frames_are_refused() -> None:
    with pytest.raises(ValueError, match="lack columns"):
        check_predictions(frame().drop(columns="train_end"), embargo=H)
    with pytest.raises(NaiveTimestampError):
        check_predictions(frame(index=T.tz_localize(None)), embargo=H)
    with pytest.raises(ValueError, match="unique and increasing"):
        check_predictions(frame(index=T[::-1]), embargo=H)
    with pytest.raises(NaiveTimestampError, match="train_end"):
        check_predictions(frame(train_end=pd.Timestamp("2024-03-12 08:00")), embargo=H)
