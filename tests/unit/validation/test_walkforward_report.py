"""WF-005: the walk-forward report gives per-fold metrics, the fold Sharpe distribution and a
decay regression that finds a planted decay and none in a stable edge."""

import json
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from xq.backtest.metrics import return_metrics
from xq.core.time import trading_day_bounds
from xq.validation.walkforward_report import FoldWindow, assign_days, walk_forward_report

FIRST = date(2024, 1, 2)


def weekdays(n: int) -> list[date]:
    days, day = [], FIRST
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def folds_over(days: list[date], size: int) -> list[FoldWindow]:
    """Folds of `size` consecutive days, windows from the first day's start to the next fold's."""
    out = []
    for k in range(0, len(days), size):
        start = trading_day_bounds(days[k])[0]
        end = trading_day_bounds(days[min(k + size, len(days) - 1)])[0]
        if k + size >= len(days):
            end = trading_day_bounds(days[-1])[1]
        out.append(FoldWindow(f"f{k // size:03d}", start - pd.Timedelta(hours=1), start, end))
    return out


def test_per_fold_metrics_and_the_sharpe_distribution() -> None:
    days = weekdays(60)
    rng = np.random.default_rng(1)
    returns = pd.Series(rng.normal(2e-4, 1e-3, 60), index=days)
    folds = folds_over(days, 10)
    report = walk_forward_report("s", returns, folds, periods_per_year=252)
    assert list(report.folds["fold_id"]) == [f.fold_id for f in folds]
    assert (report.folds["days"] == 10).all()
    first = returns.iloc[:10]
    expected = return_metrics(first.reset_index(drop=True), 252)["sharpe"]
    assert report.folds["sharpe"].iloc[0] == pytest.approx(expected)
    assert report.folds["mean_daily_bps"].iloc[0] == pytest.approx(first.mean() * 1e4)
    assert report.folds["net_return"].iloc[0] == pytest.approx(first.sum())
    assert report.folds["positive_days"].iloc[0] == pytest.approx((first > 0).mean())
    sharpe = report.folds["sharpe"].to_numpy()
    d = report.distribution
    assert d["n_folds"] == 6
    assert d["median"] == pytest.approx(np.median(sharpe))
    assert d["std"] == pytest.approx(np.std(sharpe, ddof=1))
    assert d["positive_share"] == pytest.approx((sharpe > 0).mean())
    assert report.decay is not None
    assert report.decay.n_folds == 6


def test_the_decay_regression_finds_a_planted_decay_and_not_a_stable_edge() -> None:
    days = weekdays(400)
    rng = np.random.default_rng(2)
    drift = np.linspace(20e-4, -10e-4, 400)  # 20 bp a day falling to -10 bp
    decaying = pd.Series(drift + rng.normal(0, 5e-4, 400), index=days)
    stable = pd.Series(5e-4 + rng.normal(0, 5e-4, 400), index=days)
    folds = folds_over(days, 40)
    falling = walk_forward_report("decaying", decaying, folds, periods_per_year=252).decay
    steady = walk_forward_report("stable", stable, folds, periods_per_year=252).decay
    assert falling is not None
    assert steady is not None
    assert falling.slope_per_year < 0
    assert falling.p_value < 0.01
    assert steady.p_value > 0.05


def test_too_few_folds_leave_the_regression_out_and_say_why() -> None:
    days = weekdays(30)
    returns = pd.Series(1e-4, index=days) + pd.Series(np.arange(30) * 1e-6, index=days)
    report = walk_forward_report("s", returns, folds_over(days, 10), periods_per_year=252)
    assert report.decay is None
    assert report.decay_note == "3 folds with out-of-sample days; the regression needs 4"
    assert "Not computed: 3 folds" in report.to_markdown()


def test_days_are_assigned_by_their_start_and_must_lie_in_a_fold() -> None:
    days = weekdays(20)
    folds = folds_over(days, 10)
    labels = assign_days(days, folds)
    assert labels == ["f000"] * 10 + ["f001"] * 10
    # a first window opening inside a trading day holds that day by its end
    late = [
        FoldWindow(
            "f000",
            folds[0].train_end,
            folds[0].test_start + pd.Timedelta(hours=5),
            folds[0].test_end,
        )
    ]
    assert assign_days(days[:1], late) == ["f000"]
    outside = pd.Series(1e-4, index=[*days, date(2024, 6, 3)])
    with pytest.raises(ValueError, match="lies in no fold"):
        walk_forward_report("s", outside, folds, periods_per_year=252)
    with pytest.raises(ValueError, match="missing values"):
        walk_forward_report("s", pd.Series([np.nan] * 20, index=days), folds, periods_per_year=252)
    with pytest.raises(ValueError, match="at least one fold"):
        walk_forward_report("s", pd.Series(0.0, index=days), [], periods_per_year=252)


def test_the_report_renders_and_serializes() -> None:
    days = weekdays(50)
    rng = np.random.default_rng(3)
    returns = pd.Series(rng.normal(0, 1e-3, 50), index=[d.isoformat() for d in days])
    folds = [
        FoldWindow(
            f.fold_id, f.train_end, f.test_start, f.test_end, {"mse": 0.5, "hit_rate": math.nan}
        )
        for f in folds_over(days, 10)
    ]
    report = walk_forward_report("tsmom@1d", returns, folds, periods_per_year=252, note="synthetic")
    text = report.to_markdown()
    for heading in (
        "## Folds",
        "## Fold Sharpe distribution",
        "## Decay regression",
        "Recorded fold metrics",
    ):
        assert heading in text
    assert "| f004 |" in text
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["subject"] == "tsmom@1d"
    assert len(payload["folds"]) == 5
    assert payload["folds"][0]["hit_rate"] is None
    assert payload["decay"]["n_folds"] == 5
