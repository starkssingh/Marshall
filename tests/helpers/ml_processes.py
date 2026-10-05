"""Synthetic processes for the ML pipeline's tests (ML-002, C-34 (1)): one without signal and one
with a planted signal, both with overlapping labels, and the walk-forward folds they run on."""

from __future__ import annotations

import numpy as np
import pandas as pd

from xq.core.seeds import make_rng
from xq.models.pipeline import FoldData, TrainedFold
from xq.validation.forecast_eval import log_loss
from xq.validation.splitters import Fold, WalkForwardConfig, WalkForwardSplitter

HORIZON = 48  # hours: each null label spans the next 48 hourly returns, so neighbours overlap
#: A deep random forest: the overconfident model of the purging demonstration.
FOREST = {"n_estimators": 100, "max_depth": None, "min_samples_leaf": 5, "max_samples": 0.5}
PLANTED_HORIZON = 6  # hours
#: The planted signal's loading on ``x_a`` against forward noise of standard deviation sqrt(6).
PLANTED_BETA = 1.0


def no_signal(n: int = 2400, seed: int = 9) -> FoldData:
    """Hourly samples: two slowly drifting inputs unrelated to the returns, and the sign of the
    next 48 hourly returns as the label (overlapping labels, no signal)."""
    rng = make_rng(seed)
    times = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    returns = rng.normal(size=n + HORIZON)
    forward = np.array([returns[t + 1 : t + 1 + HORIZON].sum() for t in range(n)])
    y = pd.Series((forward > 0).astype(float), index=times)
    y.iloc[-HORIZON:] = np.nan  # the data ends before these labels do
    drift = np.zeros((n, 2))
    shocks = rng.normal(size=(n, 2))
    for t in range(1, n):
        drift[t] = 0.995 * drift[t - 1] + shocks[t]
    x = pd.DataFrame(drift, index=times, columns=["drift_a", "drift_b"])
    label_end = pd.Series(times + pd.Timedelta(hours=HORIZON), index=times)
    return FoldData(x, y, label_end)


def planted_signal(n: int = 3000, seed: int = 9) -> FoldData:
    """Hourly samples: two iid standard-normal inputs; the label is the sign of
    ``PLANTED_BETA * x_a`` plus the next six iid standard-normal hourly returns (overlapping
    labels; ``x_b`` is noise)."""
    rng = make_rng(seed)
    times = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    x = pd.DataFrame(rng.normal(size=(n, 2)), index=times, columns=["x_a", "x_b"])
    returns = rng.normal(size=n + PLANTED_HORIZON)
    noise = np.array([returns[t + 1 : t + 1 + PLANTED_HORIZON].sum() for t in range(n)])
    y = pd.Series((PLANTED_BETA * x["x_a"].to_numpy() + noise > 0).astype(float), index=times)
    y.iloc[-PLANTED_HORIZON:] = np.nan
    label_end = pd.Series(times + pd.Timedelta(hours=PLANTED_HORIZON), index=times)
    return FoldData(x, y, label_end)


def walk_forward_folds(data: FoldData, embargo: str = "3D") -> list[Fold]:
    """Expanding walk-forward folds: 30 days' minimum training, 10-day test windows."""
    config = WalkForwardConfig.model_validate(
        {"min_train": "30D", "test_len": "10D", "embargo": embargo}
    )
    return WalkForwardSplitter(config).split(pd.DatetimeIndex(data.x.index), data.label_end)


def against_climatology(predictions: pd.DataFrame, folds: list[TrainedFold]) -> tuple[float, float]:
    """The stitched test log loss of ``p_cal`` and of climatology (each fold's training base rate)
    over the test rows with a known outcome and a prediction."""
    oos = predictions.dropna(subset=["y_true", "p_cal"])
    base = {f.fold_id: f.base_rate for f in folds}
    climatology = oos["fold_id"].map(base).astype(float)
    return log_loss(oos["y_true"], oos["p_cal"]), log_loss(oos["y_true"], climatology)
