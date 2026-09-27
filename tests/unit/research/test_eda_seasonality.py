"""EDA-004: injected hour-of-week effects are found with corrected intervals and labelled stable;
noise stays within the family-wise error rate; an effect that stops half-way is unstable."""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
from scipy.stats import t as student_t

from helpers.pipeline import REPO, config
from helpers.simulate import hourly_returns
from xq.core.config import EventWindow
from xq.research.eda.seasonality import (
    INSUFFICIENT,
    STABLE,
    UNSTABLE,
    cluster_se,
    critical_value,
    day_of_week_members,
    effects,
    effects_figure,
    family_effects,
    hour_of_week_labels,
    hour_of_week_order,
    membership,
    month_clusters,
    month_members,
    one_hot,
    split_halves,
    week_clusters,
    windows_config,
)

TUESDAY_10 = "Tue 10"
WEDNESDAY_08 = "Wed 08"


def hour_effects(frame: pd.DataFrame, alpha: float = 0.05) -> pd.DataFrame:
    labels = hour_of_week_labels(frame["bar_start"])
    order = [h for h in hour_of_week_order() if (labels == h).any()]
    return family_effects(
        frame,
        one_hot(labels, order),
        week_clusters(frame["trading_day"]),
        alpha=alpha,
        family="hour_of_week",
    )


def row(table: pd.DataFrame, variable: str, bucket: str) -> pd.Series:
    chosen = table.loc[(table["variable"] == variable) & (table["bucket"] == bucket)]
    assert len(chosen) == 1
    return chosen.iloc[0]


def test_injected_effects_are_significant_and_stable() -> None:
    frame = hourly_returns(
        200,
        seed=1,
        mean_effects={TUESDAY_10: 5.0},
        vol_multipliers={WEDNESDAY_08: 2.5},
    )
    table = hour_effects(frame)
    mean_effect = row(table, "ret_bps", TUESDAY_10)
    assert mean_effect["significant"]
    assert mean_effect["stability"] == STABLE
    assert mean_effect["ci_low"] < 5.0 < mean_effect["ci_high"]
    assert mean_effect["effect_sd"] == pytest.approx(0.5, abs=0.2)
    vol_effect = row(table, "abs_ret_bps", WEDNESDAY_08)
    assert vol_effect["significant"]
    assert vol_effect["stability"] == STABLE
    assert vol_effect["effect"] > 0
    # Noise buckets: Bonferroni keeps the family-wise error at 5 %.
    returns = table.loc[(table["variable"] == "ret_bps") & (table["bucket"] != TUESDAY_10)]
    assert int(returns["significant"].sum()) <= 1
    assert len(table.loc[table["variable"] == "ret_bps"]) == 5 * 24 - 8


def test_noise_alone_is_rarely_significant() -> None:
    false_positives = 0
    for seed in range(10):
        table = hour_effects(hourly_returns(60, seed=100 + seed))
        false_positives += int(table.loc[table["variable"] == "ret_bps", "significant"].any())
    assert false_positives <= 2  # family-wise 5 % per sample


def test_an_effect_that_stops_half_way_is_unstable() -> None:
    frame = hourly_returns(200, seed=2, first_half_only={TUESDAY_10: 10.0})
    effect = row(hour_effects(frame), "ret_bps", TUESDAY_10)
    assert effect["effect_first_half"] > 7
    assert abs(effect["effect_second_half"]) < 3
    assert effect["stability"] == UNSTABLE


def test_cluster_se_reduces_to_the_iid_se_with_one_observation_per_cluster() -> None:
    x = np.random.default_rng(3).standard_normal(500)
    se = cluster_se(x, np.arange(500, dtype=np.int64))
    assert se == pytest.approx(np.std(x, ddof=1) / math.sqrt(500), rel=1e-12)
    assert math.isnan(cluster_se(x, np.zeros(500, dtype=np.int64)))


def test_cluster_se_grows_with_shocks_shared_within_clusters() -> None:
    rng = np.random.default_rng(4)
    clusters = np.repeat(np.arange(100, dtype=np.int64), 20)
    x = rng.standard_normal(2000) + np.repeat(rng.standard_normal(100), 20)
    naive = np.std(x, ddof=1) / math.sqrt(len(x))
    assert cluster_se(x, clusters) > 2.5 * naive


def test_critical_values_use_student_t_with_clusters_minus_one() -> None:
    assert critical_value(0.05, 11) == pytest.approx(student_t.ppf(0.975, 10))
    assert math.isnan(critical_value(0.05, 1))
    assert critical_value(0.05, 3) > critical_value(0.05, 300)


def test_few_clusters_are_not_significant_and_halves_need_clusters() -> None:
    frame = hourly_returns(2, seed=5, mean_effects={TUESDAY_10: 5.0})
    table = hour_effects(frame)
    assert not table["significant"].any()
    assert set(table["stability"]) == {INSUFFICIENT}


def test_hour_of_week_is_new_york_local_across_dst() -> None:
    starts = pd.Series(
        pd.to_datetime(["2024-03-08T14:00:00", "2024-03-11T13:00:00"], utc=True)
    )  # 09:00 EST on Friday, 09:00 EDT on Monday
    assert hour_of_week_labels(starts).tolist() == ["Fri 09", "Mon 09"]
    order = hour_of_week_order()
    assert order[0] == "Mon 00"
    assert len(order) == 168


def test_calendar_buckets_and_clusters() -> None:
    days = pd.Series([date(2024, 1, 1), date(2024, 1, 5), date(2024, 1, 8), date(2024, 2, 1)])
    assert week_clusters(days).tolist() == [202401, 202401, 202402, 202405]
    assert month_clusters(days).tolist() == [202401, 202401, 202401, 202402]
    assert split_halves(days).tolist() == [False, False, True, True]
    assert list(day_of_week_members(days).columns) == ["Mon", "Thu", "Fri"]
    assert list(month_members(days).columns) == ["Jan", "Feb"]


def test_session_and_event_window_membership() -> None:
    sessions = config(REPO).sessions_config()
    windows = windows_config(sessions, {"lbma_pm": EventWindow(before_min=5, after_min=30)})
    starts = pd.Series(
        pd.to_datetime(
            ["2024-03-12T13:00:00", "2024-03-12T14:55:00", "2024-03-12T15:30:00"], utc=True
        )
    )
    inside = membership(starts, windows)
    # 09:00 New York (EDT) is 13:00 London (GMT): both sessions and their overlap.
    assert inside.loc[0, "new_york"]
    assert inside.loc[0, "london_new_york"]
    assert not inside.loc[0, "tokyo"]
    # LBMA PM auction at 15:00 London: [14:55, 15:30) UTC in March before the UK change.
    assert inside["lbma_pm"].tolist() == [False, True, False]
    # The US data release window (-5 / +30 around 08:30 New York = 12:30 UTC) is the dataset's.
    assert "us_data_release" in inside.columns
    assert "rollover" in inside.columns


def test_effects_ignore_missing_values_and_render() -> None:
    frame = hourly_returns(30, seed=6)
    frame.loc[frame.index[:5], "spread_bps"] = np.nan
    table = hour_effects(frame)
    spread = table.loc[table["variable"] == "spread_bps"]
    assert spread["n"].sum() == len(frame) - 5
    figure = effects_figure(table, ["ret_bps", "abs_ret_bps"], "t")
    assert len(figure.axes) == 2


def test_membership_effects_are_against_the_overall_mean() -> None:
    values = pd.Series([1.0, 1.0, 3.0, 3.0])
    members = pd.DataFrame({"a": [True, True, False, False], "b": [True, True, True, True]})
    clusters = pd.Series([1, 2, 3, 4])
    halves = pd.Series([False, True, False, True])
    table = effects(values, members, clusters, halves, alpha=0.05, family="f", variable="v")
    assert table.set_index("bucket")["effect"].to_dict() == {"a": -1.0, "b": 0.0}
