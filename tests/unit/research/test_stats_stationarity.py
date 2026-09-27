"""STAT-001 recovery: the stationarity battery on simulated processes with known answers.

A random walk has a unit root (ADF does not reject; the joint verdict is ``unit_root``), a
stationary AR(1) is stationary, and a level shift is found by Zivot-Andrews near its true date.
The size of ADF on random walks stays near its level over many draws.
"""

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from helpers.simulate import ar1, level_shift, random_walk
from xq.core.config import StationarityConfig, load_config
from xq.research.stats.results import StatResult
from xq.research.stats.stationarity import adf_test, joint_verdict, stationarity_battery

CFG = StationarityConfig(
    trend="c", adf_lag_method="aic", kpss_trends=["c", "ct"], zivot_andrews_trim=0.15
)
ALPHA = 0.05


def test_the_committed_configuration_loads() -> None:
    cfg = load_config("dev", config_dir=REPO / "config")
    stats = cfg.stats_config()
    assert stats.alpha == ALPHA
    assert stats.stationarity.kpss_trends == ["c", "ct"]


def test_random_walk_adf_does_not_reject_and_the_verdict_is_unit_root() -> None:
    x = random_walk(2000, seed=11)
    battery = stationarity_battery(x, "log_price", CFG, ALPHA)
    assert not battery.result("ADF").reject
    assert not battery.result("PP").reject
    assert battery.result("KPSS(c)").reject
    assert battery.verdict == "unit_root"
    assert battery.result("ADF").null == "the series has a unit root"


def test_adf_size_on_random_walks_is_near_its_level() -> None:
    rejections = [
        adf_test(
            random_walk(400, seed=s), "rw", trend="c", method="aic", max_lags=None, alpha=ALPHA
        ).reject
        for s in range(200)
    ]
    assert 0.01 <= np.mean(rejections) <= 0.10


def test_stationary_ar1_is_stationary_and_its_returns_too() -> None:
    battery = stationarity_battery(ar1(2000, 0.5, seed=3), "ar1", CFG, ALPHA)
    assert battery.result("ADF").reject
    assert battery.result("PP").reject
    assert not battery.result("KPSS(c)").reject
    assert battery.verdict == "stationary"
    returns = np.diff(random_walk(2001, seed=5))
    assert stationarity_battery(returns, "log_returns", CFG, ALPHA).verdict == "stationary"


def test_zivot_andrews_finds_a_level_shift_near_its_date() -> None:
    index = pd.date_range("2022-01-03 22:00", periods=1200, freq="D", tz="UTC")
    x = pd.Series(level_shift(1200, at=700, shift=4.0, seed=8), index=index)
    battery = stationarity_battery(x, "shifted", CFG, ALPHA)
    za = battery.result("ZA")
    assert za.reject
    assert abs(za.details["break_position"] - 700) <= 10
    assert battery.break_date == index[za.details["break_position"]]
    assert isinstance(battery.break_date, pd.Timestamp)


def _result(test: str, reject: bool) -> StatResult:
    return StatResult(test, "x", 0.0, 0.01 if reject else 0.5, 0, 100, "h0", "h1", ALPHA)


@pytest.mark.parametrize(
    ("adf", "pp", "kpss", "expected"),
    [
        (True, True, False, "stationary"),
        (False, False, True, "unit_root"),
        (True, True, True, "conflicting"),
        (False, False, False, "inconclusive"),
        (True, False, False, "mixed"),
        (False, True, True, "mixed"),
    ],
)
def test_joint_verdict_table(adf: bool, pp: bool, kpss: bool, expected: str) -> None:
    verdict, text = joint_verdict(_result("ADF", adf), _result("PP", pp), _result("KPSS(c)", kpss))
    assert verdict == expected
    assert text


def test_a_break_is_mentioned_only_when_zivot_andrews_rejects() -> None:
    za = StatResult("ZA", "x", -6.0, 0.001, 1, 100, "h0", "h1", ALPHA, details={"break_date": 7})
    _, text = joint_verdict(_result("ADF", False), _result("PP", False), _result("KPSS", True), za)
    assert "break" in text
    assert "7" in text
    quiet = StatResult("ZA", "x", -2.0, 0.6, 1, 100, "h0", "h1", ALPHA)
    _, text = joint_verdict(
        _result("ADF", False), _result("PP", False), _result("KPSS", True), quiet
    )
    assert "break" not in text


def test_bad_inputs_are_refused() -> None:
    with pytest.raises(ValueError, match="missing or infinite"):
        stationarity_battery(np.r_[np.nan, np.arange(100.0)], "x", CFG, ALPHA)
    with pytest.raises(ValueError, match="at least 50"):
        stationarity_battery(np.arange(10.0), "x", CFG, ALPHA)
    with pytest.raises(ValueError, match="constant"):
        stationarity_battery(np.ones(100), "x", CFG, ALPHA)


def test_the_table_has_one_row_per_test() -> None:
    table = stationarity_battery(random_walk(300, seed=1), "rw", CFG, ALPHA).table()
    assert list(table["test"]) == ["ADF", "PP", "KPSS(c)", "KPSS(ct)", "ZA"]
    assert {"statistic", "p_value", "lags", "reject", "null"} <= set(table.columns)
