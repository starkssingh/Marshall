"""ML-002: the in-fold pipeline. The purging demonstration (the plan's test): on synthetic data
with overlapping labels and no signal, unpurged shuffled cross-validation shows spurious skill,
while purged k-fold and the purged walk-forward pipeline rank at chance (AUC near 0.5) and the
pipeline shows no out-of-sample skill. Its log loss is worse than chance, not at chance: see
ADR 0068, ML-002 item 4, for that and for how the thresholds were set (lowered after a
three-seed exploratory run)."""

import math
from datetime import date

import numpy as np
import pandas as pd
from sklearn.model_selection import KFold

from helpers.pipeline import REPO
from xq.core.config import MlPipelineConfig, load_config
from xq.core.seeds import make_rng
from xq.models.base import build_forecaster
from xq.models.pipeline import (
    FoldData,
    default_embargo,
    inner_cv_loss,
    inner_splits,
    run_pipeline,
    split_validation,
    train_fold,
)
from xq.validation.forecast_eval import auc, log_loss
from xq.validation.splitters import PurgedKFold, WalkForwardConfig, WalkForwardSplitter

CFG = load_config("research", config_dir=REPO / "config")
PIPELINE = CFG.ml_config().pipeline
HORIZON = 48  # hours: each label spans the next 48 hourly returns, so neighbours overlap
FOREST = {"n_estimators": 100, "max_depth": None, "min_samples_leaf": 5, "max_samples": 0.5}


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


def walk_forward_folds(data: FoldData, embargo: str = "3D") -> list:
    config = WalkForwardConfig.model_validate(
        {"min_train": "30D", "test_len": "10D", "embargo": embargo}
    )
    return WalkForwardSplitter(config).split(pd.DatetimeIndex(data.x.index), data.label_end)


def test_purging_gives_chance_while_shuffled_cv_shows_spurious_skill() -> None:
    data = no_signal()
    known = data.y.notna().to_numpy()
    x, y = data.x.loc[known], data.y.loc[known]
    climatology = log_loss(y.to_numpy(), np.full(len(y), y.mean()))

    # unpurged shuffled k-fold: neighbours of every test row sit in the training set
    shuffled = np.full(len(y), np.nan)
    for train, test in KFold(5, shuffle=True, random_state=0).split(x):
        model = build_forecaster("random_forest", FOREST, seed=1).fit(x.iloc[train], y.iloc[train])
        shuffled[test] = model.predict_proba(x.iloc[test])
    shuffled_loss = log_loss(y.to_numpy(), shuffled)
    assert auc(y.to_numpy(), shuffled) > 0.7
    assert shuffled_loss < climatology - 0.1  # spurious skill

    # purged k-fold with embargo over the same rows: chance
    times = pd.DatetimeIndex(x.index)
    splits = PurgedKFold(5, pd.Timedelta(hours=HORIZON)).split(times, data.label_end.loc[known])
    purged = np.full(len(y), np.nan)
    for split in splits:
        model = build_forecaster("random_forest", FOREST, seed=1)
        model.fit(x.iloc[split.train_idx], y.iloc[split.train_idx])
        purged[split.test_idx] = model.predict_proba(x.iloc[split.test_idx])
    assert abs(auc(y.to_numpy(), purged) - 0.5) < 0.1

    # the walk-forward pipeline (purged, embargoed, calibrated on validation): chance level
    embargo = pd.Timedelta(hours=HORIZON)
    output = run_pipeline(
        "random_forest", data, walk_forward_folds(data), PIPELINE, embargo=embargo, seed=3,
        params=FOREST,
    )  # fmt: skip
    oos = output.predictions.dropna(subset=["y_true", "p_cal"])
    assert len(oos) > 1000
    oos_climatology = log_loss(oos["y_true"], np.full(len(oos), oos["y_true"].mean()))
    pipeline_loss = log_loss(oos["y_true"], oos["p_cal"])
    assert pipeline_loss > oos_climatology - 0.01  # no skill out of sample
    assert abs(auc(oos["y_true"], oos["p_cal"]) - 0.5) < 0.1
    assert pipeline_loss > shuffled_loss + 0.1


def test_validation_split_purges_fitting_rows_against_the_validation_start() -> None:
    times = pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC")
    ends = (times + pd.Timedelta(hours=5)).as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
    fit, val = split_validation(times, ends, np.arange(100), 0.2, pd.Timedelta(hours=2))
    np.testing.assert_array_equal(val, np.arange(80, 100))
    # a fitting label must end before 80h - 2h: rows up to 72 (ending at 77h)
    np.testing.assert_array_equal(fit, np.arange(0, 73))


def test_the_default_embargo_is_the_horizon_or_one_trading_day() -> None:
    sessions = CFG.sessions_config()
    assert default_embargo(pd.Timedelta(hours=1), sessions) == pd.Timedelta(hours=23)
    assert default_embargo(pd.Timedelta(days=2), sessions) == pd.Timedelta(days=2)


def test_transforms_and_calibration_never_see_test_rows() -> None:
    data = no_signal(n=1500, seed=4)
    fold = walk_forward_folds(data)[0]
    embargo = pd.Timedelta(hours=HORIZON)
    base = train_fold("logistic", fold, data, PIPELINE, embargo=embargo, seed=5, params={"C": 1.0})
    assert base is not None
    # change every test row's inputs and labels, and every row after the fold
    later = pd.DatetimeIndex(data.x.index) >= fold.test_start
    changed = FoldData(
        data.x.mask(
            pd.DataFrame(dict.fromkeys(data.x.columns, later), index=data.x.index), data.x * 10 + 3
        ),
        data.y.where(~later, 1 - data.y),
        data.label_end,
    )
    again = train_fold(
        "logistic", fold, changed, PIPELINE, embargo=embargo, seed=5, params={"C": 1.0}
    )
    assert again is not None
    pd.testing.assert_series_equal(base.scaler.mean_, again.scaler.mean_)
    assert base.val_metrics == again.val_metrics
    probe = np.linspace(0.01, 0.99, 25)
    assert base.calibrator is not None
    assert again.calibrator is not None
    np.testing.assert_array_equal(
        base.calibrator.transform(probe), again.calibrator.transform(probe)
    )
    # the scaler is the fitting rows' own
    np.testing.assert_allclose(base.scaler.mean_, data.x.loc[base.fit_index].mean())
    assert base.fit_index.max() < base.val_index.min()
    assert (data.label_end.loc[base.fit_index] < base.val_index.min() - embargo).all()
    assert base.train_end < fold.test_start
    assert (base.predictions.index >= fold.test_start).all()


def test_a_fixed_seed_reproduces_the_pipeline_and_missing_inputs_stay_unpredicted() -> None:
    data = no_signal(n=1500, seed=6)
    x = data.x.copy()
    x.iloc[1300, 0] = np.nan  # a test row with a missing input
    data = FoldData(x, data.y, data.label_end)
    folds = walk_forward_folds(data)
    embargo = pd.Timedelta(hours=HORIZON)
    one = run_pipeline(
        "random_forest", data, folds, PIPELINE, embargo=embargo, seed=8, params=FOREST
    )
    two = run_pipeline(
        "random_forest", data, folds, PIPELINE, embargo=embargo, seed=8, params=FOREST
    )
    pd.testing.assert_frame_equal(one.predictions, two.predictions)
    row = data.x.index[1300]
    assert row in one.predictions.index
    assert np.isnan(one.predictions.loc[row, "p_raw"])
    assert one.predictions["p_raw"].notna().sum() > 0


def test_inner_cv_is_purged_and_embargoed_inside_the_fitting_rows() -> None:
    data = no_signal(n=800, seed=7)
    known = data.y.notna().to_numpy()
    times = pd.DatetimeIndex(data.x.index[known])
    ends = data.label_end.loc[known]
    embargo = pd.Timedelta(hours=HORIZON)
    for split in inner_splits(times, ends, None, PIPELINE.inner_splits, embargo):
        first, last = times[split.test_idx[0]], ends.iloc[split.test_idx].max()
        train_times, train_ends = times[split.train_idx], ends.iloc[split.train_idx]
        overlapping = (train_ends >= first) & (train_times <= last + embargo)
        assert not overlapping.any()
    loss = inner_cv_loss(
        "logistic", {"C": 1.0}, data.x.loc[known], data.y.loc[known], None,
        inner_splits(times, ends, None, 5, embargo), seed=1,
    )  # fmt: skip
    assert math.isfinite(loss)
    assert loss > 0.6  # no signal: near ln 2 on purged inner folds


def test_too_few_fitting_rows_skip_the_fold() -> None:
    data = no_signal(n=1500, seed=10)
    folds = walk_forward_folds(data)
    strict = PIPELINE.model_copy(update={"min_training_rows": 100_000})
    out = run_pipeline(
        "logistic", data, folds, strict, embargo=pd.Timedelta(hours=48), seed=1, params={}
    )
    assert out.folds == []
    assert out.skipped == [f.fold_id for f in folds]
    assert isinstance(PIPELINE, MlPipelineConfig)
    assert date(2024, 1, 1) <= data.x.index[0].date()
