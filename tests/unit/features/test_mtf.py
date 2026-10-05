"""FEAT-008: a higher-timeframe value appears only after that bar's ``available_at`` (synthetic
availability test, with a publication latency), never by bar start."""

import numpy as np
import pandas as pd
import pytest

from helpers.features import SESSIONS, hand_bars
from xq.core.config import FeatureInstanceConfig, FeatureSetConfig
from xq.core.types import Timeframe
from xq.datasets.base_features import FeatureContext
from xq.features.mtf import join_on_availability
from xq.features.registry import compute_feature_set, feature_specs

CONTEXT = FeatureContext(Timeframe.M15, (Timeframe.H1, Timeframe.D1), SESSIONS)
LATENCY = pd.Timedelta(minutes=5)


def inputs() -> dict[str, pd.DataFrame]:
    """15m bars from 08:00 to 14:00 UTC, available at their end; hourly bars whose closes are
    1, 2, 3, ... (bar k starts at 08:00 + k hours), published five minutes after they end."""
    base = hand_bars([2000.0] * 24, start="2024-03-12 08:00")
    hourly = hand_bars(
        [float(k + 1) for k in range(6)], start="2024-03-12 08:00", timeframe=Timeframe.H1
    )
    hourly["available_at_utc"] = hourly["available_at_utc"] + LATENCY
    return {"base": base, "1h": hourly}


def hourly_close(timeframe: str = "1h") -> list[FeatureInstanceConfig]:
    return [FeatureInstanceConfig(feature="log_return", params={"bars": 1}, timeframe=timeframe)]


def test_a_higher_timeframe_value_appears_only_after_its_bar_is_available() -> None:
    data = inputs()
    specs = feature_specs(FeatureSetConfig(features=hourly_close()))
    out = compute_feature_set(specs, data, CONTEXT)
    assert list(out.columns) == ["mtf_1h_log_return_1", "mtf_1h_available_at"]
    provenance = out["mtf_1h_available_at"]
    assert (provenance.dropna() <= provenance.dropna().index).all()
    # the 10:00-11:00 bar (close 3) is published at 11:05: the 11:00 decision still reads the
    # 09:00-10:00 bar (close 2, published 10:05), the 11:15 decision reads it
    at_11 = pd.Timestamp("2024-03-12 11:00", tz="UTC")
    at_1115 = pd.Timestamp("2024-03-12 11:15", tz="UTC")
    assert provenance.loc[at_11] == pd.Timestamp("2024-03-12 10:05", tz="UTC")
    assert out.loc[at_11, "mtf_1h_log_return_1"] == pytest.approx(np.log(2 / 1))
    assert provenance.loc[at_1115] == pd.Timestamp("2024-03-12 11:05", tz="UTC")
    assert out.loc[at_1115, "mtf_1h_log_return_1"] == pytest.approx(np.log(3 / 2))
    # before the first hourly return is published (09:05 + an hour) there is nothing to read
    assert out.loc[: pd.Timestamp("2024-03-12 10:00", tz="UTC"), "mtf_1h_log_return_1"].isna().all()


def test_every_switch_happens_at_the_first_decision_at_or_after_an_availability() -> None:
    data = inputs()
    out = compute_feature_set(
        feature_specs(FeatureSetConfig(features=hourly_close())), data, CONTEXT
    )
    decisions = out.index
    for available in pd.DatetimeIndex(data["1h"]["available_at_utc"]):
        first = decisions[decisions >= available]
        if len(first):
            assert out.loc[first[0], "mtf_1h_available_at"] == available
        before = decisions[decisions < available]
        if len(before):
            previous = out.loc[before[-1], "mtf_1h_available_at"]
            assert pd.isna(previous) or previous < available


def test_values_do_not_depend_on_unpublished_bars() -> None:
    data = inputs()
    specs = feature_specs(FeatureSetConfig(features=hourly_close()))
    full = compute_feature_set(specs, data, CONTEXT)
    cut = pd.Timestamp("2024-03-12 11:00", tz="UTC")
    changed = {**data, "1h": data["1h"].copy()}
    unpublished = (changed["1h"]["available_at_utc"] > cut).to_numpy()
    changed["1h"].loc[unpublished, "close"] *= 1.5
    again = compute_feature_set(specs, changed, CONTEXT)
    pd.testing.assert_frame_equal(full.loc[:cut], again.loc[:cut])


def test_a_daily_bar_is_read_only_after_the_trading_day_closes() -> None:
    days = hand_bars(
        [10.0, 11.0, 12.0], starts=["2024-03-10 21:00", "2024-03-11 21:00", "2024-03-12 21:00"],
        timeframe=Timeframe.D1,
    )  # fmt: skip
    base = hand_bars([2000.0] * 8, start="2024-03-12 19:00")  # to 21:00 UTC and beyond
    specs = feature_specs(FeatureSetConfig(features=hourly_close("1d")))
    out = compute_feature_set(specs, {"base": base, "1d": days}, CONTEXT)
    # the trading day of 2024-03-12 ends at 21:00 UTC (17:00 New York, EDT): its bar is the
    # latest available at and after that decision, never before
    roll = pd.Timestamp("2024-03-12 21:00", tz="UTC")
    assert out.loc[: roll - pd.Timedelta(minutes=15), "mtf_1d_log_return_1"].isna().all()
    assert out.loc[roll, "mtf_1d_available_at"] == roll
    assert out.loc[roll, "mtf_1d_log_return_1"] == pytest.approx(np.log(11 / 10))


def test_the_join_without_any_available_bar() -> None:
    decisions = pd.DatetimeIndex([pd.Timestamp("2024-03-12 10:00", tz="UTC")])
    empty = pd.DataFrame({"x": pd.Series(dtype="float64")}, index=pd.DatetimeIndex([], tz="UTC"))
    out = join_on_availability(decisions, empty, "mtf_1h_")
    assert out["mtf_1h_x"].isna().all()
    assert out["mtf_1h_available_at"].isna().all()
