"""DQ-003: bar-level checks detect exactly the injected defects; a clean day passes."""

from datetime import date

import pandas as pd
import pytest

from helpers.quality import partition, repo_config
from helpers.ticks import dense_ticks
from xq.core.config import AppConfig
from xq.quality.registry import CheckResult, PartitionData, Status, evaluate, load_builtin_checks

DAY = date(2024, 3, 12)  # London 08:00-17:00 UTC (still GMT), New York 12:00-21:00 UTC (EDT)
BAR_CHECKS = [
    "bar.ohlc_consistency",
    "bar.missing_minutes",
    "bar.duplicate_starts",
    "bar.extreme_returns",
    "bar.zero_range",
    "bar.basis_consistency",
]
MINUTE = 60 * 10**9


def ns(text: str) -> int:
    return int(pd.Timestamp(text, tz="UTC").value)


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    load_builtin_checks()
    return repo_config()


@pytest.fixture(scope="module")
def base() -> pd.DataFrame:
    # 3-second ticks, so every session minute has ticks and a range.
    return dense_ticks("2024-03-11 22:00", "2024-03-12 21:00", seed=41, mean_interval_s=3)


@pytest.fixture(scope="module")
def clean_day(base: pd.DataFrame) -> PartitionData:
    return partition(base, DAY)


def run(check_id: str, data: PartitionData, cfg: AppConfig) -> CheckResult:
    result = evaluate(load_builtin_checks().get(check_id), data, cfg.quality_config())
    assert result is not None
    return result


def with_bars(data: PartitionData, bars: pd.DataFrame) -> PartitionData:
    return partition(data.ticks, DAY, clean=False, bars_1m=bars)


@pytest.mark.parametrize("check_id", BAR_CHECKS)
def test_clean_day_passes(check_id: str, clean_day: PartitionData, cfg: AppConfig) -> None:
    result = run(check_id, clean_day, cfg)
    assert result.status is Status.PASS, (check_id, result.metric)
    assert result.metric == 0


def test_ohlc_inconsistency(clean_day: PartitionData, cfg: AppConfig) -> None:
    bars = clean_day.bars_1m.copy()
    bars.loc[10, "bid_high"] = bars.loc[10, "bid_open"] - 1.0
    bars.loc[20, "mid_low"] = bars.loc[20, "mid_close"] + 1.0
    result = run("bar.ohlc_consistency", with_bars(clean_day, bars), cfg)
    assert result.metric == 2
    assert result.status is Status.FAIL
    assert [a.ts_utc.value for a in result.anomalies] == bars.loc[
        [10, 20], "bar_start_utc"
    ].tolist()


def test_missing_minutes_in_sessions(base: pd.DataFrame, cfg: AppConfig) -> None:
    gap = (base["ts_utc"] >= ns("2024-03-12 14:00")) & (base["ts_utc"] < ns("2024-03-12 14:30"))
    data = partition(base[~gap], DAY)
    result = run("bar.missing_minutes", data, cfg)
    session_minutes = (21 - 8) * 60  # London open 08:00 to New York close 21:00 UTC
    assert result.details == {"expected_minutes": session_minutes, "missing_minutes": 30}
    assert result.metric == pytest.approx(30 / session_minutes)
    assert result.status is Status.WARN  # 3.8%: above 1%, under 5%
    assert result.anomalies[0].ts_utc == pd.Timestamp("2024-03-12 14:00", tz="UTC")
    assert result.anomalies[0].value == 30


def test_missing_minutes_outside_sessions_do_not_count(base: pd.DataFrame, cfg: AppConfig) -> None:
    night = (base["ts_utc"] >= ns("2024-03-12 02:00")) & (base["ts_utc"] < ns("2024-03-12 03:00"))
    assert run("bar.missing_minutes", partition(base[~night], DAY), cfg).metric == 0


def test_duplicate_bar_starts(clean_day: PartitionData, cfg: AppConfig) -> None:
    bars = pd.concat([clean_day.bars_1m, clean_day.bars_1m.iloc[[5, 6, 7]]], ignore_index=True)
    bars = bars.sort_values("bar_start_utc", kind="stable", ignore_index=True)
    result = run("bar.duplicate_starts", with_bars(clean_day, bars), cfg)
    assert result.metric == 3
    assert result.status is Status.FAIL


def test_extreme_returns(base: pd.DataFrame, cfg: AppConfig) -> None:
    # The last tick of the 13:00 minute jumps 8.00, the 13:01 minute is back to normal: two
    # extreme adjacent-minute returns, each labelled with the bar whose close moved.
    frame = base.copy()
    minute = (frame["ts_utc"] >= ns("2024-03-12 13:00")) & (
        frame["ts_utc"] < ns("2024-03-12 13:01")
    )
    last = frame.index[minute][-1]
    frame.loc[last, ["bid", "ask"]] += 8.0
    result = run("bar.extreme_returns", partition(frame, DAY), cfg)
    assert result.metric == 2
    assert result.status is Status.PASS  # two per day is the warn level, not above it
    assert sorted(a.ts_utc for a in result.anomalies) == [
        pd.Timestamp("2024-03-12 13:00", tz="UTC"),
        pd.Timestamp("2024-03-12 13:01", tz="UTC"),
    ]


def test_zero_range_bars(base: pd.DataFrame, cfg: AppConfig) -> None:
    hour = (base["ts_utc"] >= ns("2024-03-12 10:00")) & (base["ts_utc"] < ns("2024-03-12 11:00"))
    first_of_minute = base["ts_utc"] // MINUTE != (base["ts_utc"] // MINUTE).shift()
    data = partition(base[~hour | first_of_minute], DAY)  # one tick per minute in that hour
    result = run("bar.zero_range", data, cfg)
    assert result.details["zero_range"] == 60
    assert result.metric == pytest.approx(60 / result.details["bars"])
    assert result.status is Status.PASS  # 60 of 780 session bars is under 10%


def test_basis_inconsistency(clean_day: PartitionData, cfg: AppConfig) -> None:
    bars = clean_day.bars_1m.copy()
    bars.loc[3, "mid_close"] += 0.05  # no longer the bid/ask average
    bars.loc[4, "ask_close"] = bars.loc[4, "bid_close"] - 0.10  # crossed close
    result = run("bar.basis_consistency", with_bars(clean_day, bars), cfg)
    assert result.metric == 2
    assert result.status is Status.FAIL


def test_empty_day_gives_no_bar_results(cfg: AppConfig) -> None:
    empty = partition(dense_ticks("2024-03-12 00:00", "2024-03-12 00:00:01"), DAY)
    for check_id in ("bar.ohlc_consistency", "bar.duplicate_starts", "bar.basis_consistency"):
        assert evaluate(load_builtin_checks().get(check_id), empty, cfg.quality_config()) is None
