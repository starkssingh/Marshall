"""DQ-004: calendar checks detect exactly the injected defects; a clean day passes."""

from datetime import date

import pandas as pd
import pytest

from helpers.quality import partition, repo_config
from helpers.ticks import dense_ticks, inject, row_like
from xq.core.config import AppConfig
from xq.quality.registry import CheckResult, PartitionData, Status, evaluate, load_builtin_checks

DAY = date(2024, 3, 12)  # market 2024-03-11 22:00 UTC to 2024-03-12 21:00 UTC
CAL_CHECKS = ["cal.closed_market_ticks", "cal.missing_open_data", "cal.gap_location"]
MINUTE = 60 * 10**9
WIDE = (
    int(pd.Timestamp("2024-01-01", tz="UTC").value),
    int(pd.Timestamp("2025-01-01", tz="UTC").value),
)


def ns(text: str) -> int:
    return int(pd.Timestamp(text, tz="UTC").value)


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    load_builtin_checks()
    return repo_config()


@pytest.fixture(scope="module")
def base() -> pd.DataFrame:
    return dense_ticks("2024-03-11 22:00", "2024-03-12 21:00", seed=51, mean_interval_s=3)


def maybe(check_id: str, data: PartitionData, cfg: AppConfig) -> CheckResult | None:
    return evaluate(load_builtin_checks().get(check_id), data, cfg.quality_config())


def run(check_id: str, data: PartitionData, cfg: AppConfig) -> CheckResult:
    result = maybe(check_id, data, cfg)
    assert result is not None
    return result


@pytest.mark.parametrize("check_id", CAL_CHECKS)
def test_clean_day_passes(check_id: str, base: pd.DataFrame, cfg: AppConfig) -> None:
    result = run(check_id, partition(base, DAY, coverage=WIDE), cfg)
    assert result.status is Status.PASS, (check_id, result.metric)
    assert result.metric < 1  # under a minute from open/close, no missing minutes, no closed ticks


def test_holiday_check_does_not_apply_on_ordinary_days(base: pd.DataFrame, cfg: AppConfig) -> None:
    assert maybe("cal.holiday_behaviour", partition(base, DAY, coverage=WIDE), cfg) is None


def test_ticks_while_closed(base: pd.DataFrame, cfg: AppConfig) -> None:
    last = len(base) - 1
    late = [
        row_like(
            base,
            last,
            ts_utc=ns("2024-03-12 21:00") + k * MINUTE,
            bid=float(base["bid"].iloc[last]) + 0.01 * (k + 1),
        )
        for k in range(3)
    ]
    frame, _ = inject(base, late)
    result = run("cal.closed_market_ticks", partition(frame, DAY, coverage=WIDE), cfg)
    assert result.details["closed_market_ticks"] == 3
    assert result.metric == pytest.approx(3 / len(frame))
    assert result.status is Status.WARN  # any, but under 0.1%


def test_ticks_on_a_weekend_day_fail(cfg: AppConfig) -> None:
    # The synthetic broker never quotes at weekends, so move two weekday hours onto Saturday.
    weekday = dense_ticks("2024-03-06 12:00", "2024-03-06 14:00", seed=3, mean_interval_s=30)
    saturday = weekday.assign(ts_utc=weekday["ts_utc"] + 3 * 24 * 60 * MINUTE)
    result = run("cal.closed_market_ticks", partition(saturday, date(2024, 3, 9)), cfg)
    assert result.metric == 1.0
    assert result.status is Status.FAIL
    assert result.details["market_open_day"] is False


def test_trading_on_a_full_close_holiday(cfg: AppConfig) -> None:
    # Good Friday 2024-03-29 is closed in the calendar; a feed that quotes it disagrees.
    good_friday = dense_ticks("2024-03-28 22:00", "2024-03-29 02:00", seed=4, mean_interval_s=10)
    data = partition(good_friday, date(2024, 3, 29))
    result = run("cal.holiday_behaviour", data, cfg)
    assert result.metric == len(good_friday)
    assert result.details["holiday"] == "Good Friday"
    assert result.status is Status.FAIL


def test_trading_after_an_early_close(cfg: AppConfig) -> None:
    # Thanksgiving 2024-11-28 closes at 13:30 New York (18:30 UTC); these ticks run to 19:00.
    day = dense_ticks("2024-11-28 17:00", "2024-11-28 19:00", seed=5, mean_interval_s=60)
    after_close = int((day["ts_utc"] >= ns("2024-11-28 18:30")).sum())
    result = run("cal.holiday_behaviour", partition(day, date(2024, 11, 28)), cfg)
    assert result.metric == after_close > 0
    assert result.details["early_close"] is True
    assert result.status is Status.WARN


def test_missing_data_while_open(base: pd.DataFrame, cfg: AppConfig) -> None:
    asia = (base["ts_utc"] >= ns("2024-03-12 01:00")) & (base["ts_utc"] < ns("2024-03-12 03:00"))
    result = run("cal.missing_open_data", partition(base[~asia], DAY, coverage=WIDE), cfg)
    assert result.details == {"expected_minutes": 23 * 60, "missing_minutes": 120}
    assert result.metric == pytest.approx(120 / (23 * 60))
    assert result.status is Status.WARN  # 8.7%: above 5%, under 20%
    assert [(a.ts_utc, a.value) for a in result.anomalies] == [
        (pd.Timestamp("2024-03-12 01:00", tz="UTC"), 60.0),
        (pd.Timestamp("2024-03-12 02:00", tz="UTC"), 60.0),
    ]


def test_clock_an_hour_off_fails_gap_location(base: pd.DataFrame, cfg: AppConfig) -> None:
    shifted = base.assign(ts_utc=base["ts_utc"] + 60 * MINUTE)  # a misdeclared clock
    result = run("cal.gap_location", partition(shifted, DAY, coverage=WIDE), cfg)
    assert result.details["open_minutes"] == pytest.approx(60, abs=1)
    assert result.details["close_minutes"] == pytest.approx(60, abs=1)
    assert result.status is Status.FAIL
    assert run("cal.closed_market_ticks", partition(shifted, DAY, coverage=WIDE), cfg).status is (
        Status.FAIL
    )


def test_late_first_tick_warns(base: pd.DataFrame, cfg: AppConfig) -> None:
    late = base[base["ts_utc"] >= ns("2024-03-11 22:15")]
    result = run("cal.gap_location", partition(late, DAY, coverage=WIDE), cfg)
    assert result.details["open_minutes"] == pytest.approx(15, abs=0.1)
    assert result.status is Status.WARN


def test_data_edges_are_not_clock_errors(base: pd.DataFrame, cfg: AppConfig) -> None:
    # The source's data starts at 02:00 UTC this day: the open boundary cannot be judged.
    edge = base[base["ts_utc"] >= ns("2024-03-12 02:00")]
    coverage = (int(edge["ts_utc"].min()), WIDE[1])
    result = run("cal.gap_location", partition(edge, DAY, coverage=coverage), cfg)
    assert "open_minutes" not in result.details
    assert result.status is Status.PASS
    missing = run("cal.missing_open_data", partition(edge, DAY, coverage=coverage), cfg)
    assert missing.metric < 0.01  # minutes before the data starts are not counted as missing
