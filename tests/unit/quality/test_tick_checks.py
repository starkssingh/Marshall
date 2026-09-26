"""DQ-002: tick-level checks detect exactly the injected defects; a clean day passes."""

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from helpers.quality import partition, repo_config
from helpers.ticks import (
    crossed,
    dense_ticks,
    exact_duplicate,
    frozen_quote,
    inject,
    non_positive,
    price_spike,
    same_time_other_price,
    wide_spread,
)
from xq.core.config import AppConfig
from xq.data.flags import TickFlag
from xq.data.spreads import SpreadHistogram
from xq.quality.checks.ticks import hourly_tick_counts
from xq.quality.registry import (
    CheckResult,
    PartitionData,
    Status,
    evaluate,
    load_builtin_checks,
)

DAY = date(2024, 3, 12)  # Tuesday: market 2024-03-11 22:00 UTC to 2024-03-12 21:00 UTC (EDT)
TICK_CHECKS = [
    "tick.ordering",
    "tick.duplicates_exact",
    "tick.duplicates_diff_price",
    "tick.nonpositive_crossed",
    "tick.spread_outliers",
    "tick.spikes",
    "tick.stale_quotes",
    "tick.rate_anomalies",
]


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    load_builtin_checks()
    return repo_config()


@pytest.fixture(scope="module")
def base() -> pd.DataFrame:
    return dense_ticks("2024-03-11 22:00", "2024-03-12 21:00", seed=31, mean_interval_s=10)


@pytest.fixture(scope="module")
def context(base: pd.DataFrame) -> dict[str, Any]:
    """Spread statistics and tick-rate norms taken from the clean day itself."""
    histogram = SpreadHistogram(0.01)
    histogram.add(base["ts_utc"].to_numpy(), (base["ask"] - base["bid"]).to_numpy())
    market = [
        (
            int(pd.Timestamp("2024-03-11 22:00", tz="UTC").value),
            int(pd.Timestamp("2024-03-12 21:00", tz="UTC").value),
        )
    ]
    norm = hourly_tick_counts(base["ts_utc"].to_numpy(), market)
    return {"spread_stats": histogram.table(), "hourly_tick_norm": norm}


def maybe(check_id: str, data: PartitionData, cfg: AppConfig) -> CheckResult | None:
    return evaluate(load_builtin_checks().get(check_id), data, cfg.quality_config())


def run(check_id: str, data: PartitionData, cfg: AppConfig) -> CheckResult:
    result = maybe(check_id, data, cfg)
    assert result is not None
    return result


def build(frame: pd.DataFrame, context: dict[str, Any], **extra: Any) -> PartitionData:
    return partition(frame, DAY, **context, **extra)


@pytest.mark.parametrize("check_id", TICK_CHECKS)
def test_clean_day_passes(
    check_id: str, base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig
) -> None:
    result = run(check_id, build(base, context), cfg)
    assert result.status is Status.PASS
    assert result.metric == 0
    assert result.anomalies == []


def test_timestamp_flags(base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig) -> None:
    frame = base.copy()
    flags = frame["flags"].to_numpy().copy()
    flags[[100, 200]] |= np.uint32(TickFlag.TS_OUT_OF_ORDER)
    flags[300] |= np.uint32(TickFlag.TS_DST_AMBIGUOUS)
    frame["flags"] = flags
    result = run("tick.ordering", build(frame, context), cfg)
    assert result.metric == pytest.approx(3 / len(frame))
    assert result.status is Status.WARN  # any is a warning; 0.04% is under the 0.1% fail level


def test_exact_duplicates(base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig) -> None:
    frame, ids = inject(base, [exact_duplicate(base, i) for i in (1000, 2000, 3000)])
    result = run("tick.duplicates_exact", build(frame, context), cfg)
    assert result.metric == pytest.approx(3 / len(frame))
    assert result.status is Status.PASS  # under the 1% warn level
    assert len(result.anomalies) == len(ids)


def test_dropped_duplicates_still_count(
    base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig
) -> None:
    data = build(base, context, dropped={"DUP_EXACT": 200})
    result = run("tick.duplicates_exact", data, cfg)
    assert result.metric == pytest.approx(200 / (len(base) + 200))
    assert result.details["dropped"] == 200
    assert result.status is Status.WARN


def test_same_time_other_price(base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig) -> None:
    frame, _ = inject(base, [same_time_other_price(base, i) for i in (1000, 2000)])
    result = run("tick.duplicates_diff_price", build(frame, context), cfg)
    assert result.metric == pytest.approx(2 / len(frame))
    assert result.status is Status.PASS  # 0.02% of ticks is under the 0.1% warn level
    assert len(result.anomalies) == 2


def test_non_positive_and_crossed(
    base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig
) -> None:
    frame, _ = inject(base, [non_positive(base, 1500), crossed(base, 2500)])
    result = run("tick.nonpositive_crossed", build(frame, context), cfg)
    assert result.metric == pytest.approx(2 / len(frame))
    assert result.status is Status.FAIL  # above 0.01%
    dropped = run("tick.nonpositive_crossed", build(base, context, dropped={"CROSSED": 1}), cfg)
    assert dropped.metric == pytest.approx(1 / (len(base) + 1))


def test_spread_outliers(base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig) -> None:
    frame, _ = inject(base, [wide_spread(base, i, width=6.0) for i in (1000, 3000, 5000)])
    result = run("tick.spread_outliers", build(frame, context), cfg)
    assert result.details["outliers"] == 3
    assert result.metric == pytest.approx(3 / len(frame))
    assert result.status is Status.PASS  # 3 of ~8000 ticks is under the 0.1% warn level
    assert all(a.value > 10 for a in result.anomalies)
    assert (
        maybe("tick.spread_outliers", build(frame, {**context, "spread_stats": None}), cfg) is None
    )


def test_spikes_count_events(base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig) -> None:
    spikes = [price_spike(base, i) for i in (1000, 2500, 4000, 5500, 7000, 7500)]
    frame, _ = inject(base, spikes)
    result = run("tick.spikes", build(frame, context), cfg)
    assert result.metric == 6
    assert result.status is Status.WARN  # more than 5 per day


def test_stale_quotes_in_active_sessions(
    base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig
) -> None:
    # 2024-03-12 13:00 UTC is inside both London and New York sessions.
    start = int(np.searchsorted(base["ts_utc"], pd.Timestamp("2024-03-12 13:00", tz="UTC").value))
    frame, _ = frozen_quote(base, start, seconds=300, every=10)
    result = run("tick.stale_quotes", build(frame, context), cfg)
    assert 300 <= result.metric < 340  # the frozen 300 s plus the wait for the next change
    assert result.status is Status.WARN

    silent = base[
        ~base["ts_utc"].between(
            pd.Timestamp("2024-03-12 15:00", tz="UTC").value,
            pd.Timestamp("2024-03-12 15:40", tz="UTC").value,
        )
    ]
    result = run("tick.stale_quotes", build(silent, context), cfg)
    assert 2400 <= result.metric < 2440  # a 40-minute silence is more than 30 minutes
    assert result.status is Status.FAIL


def test_stale_quotes_outside_sessions_do_not_count(
    base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig
) -> None:
    quiet = base[
        ~base["ts_utc"].between(
            pd.Timestamp("2024-03-12 01:00", tz="UTC").value,
            pd.Timestamp("2024-03-12 02:00", tz="UTC").value,
        )
    ]
    assert run("tick.stale_quotes", build(quiet, context), cfg).metric == 0


def test_tick_rate_anomalies(base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig) -> None:
    hour = (base["ts_utc"] >= pd.Timestamp("2024-03-12 10:00", tz="UTC").value) & (
        base["ts_utc"] < pd.Timestamp("2024-03-12 11:00", tz="UTC").value
    )
    thinned = base[~hour | (np.arange(len(base)) % 20 == 0)]  # keep 5% of that hour
    result = run("tick.rate_anomalies", build(thinned, context), cfg)
    assert result.metric == 1
    assert result.details["low"] == 1
    assert result.anomalies[0].ts_utc == pd.Timestamp("2024-03-12 10:00", tz="UTC")
    no_norm = build(base, {**context, "hourly_tick_norm": None})
    assert maybe("tick.rate_anomalies", no_norm, cfg) is None


def test_checks_never_change_the_data(
    base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig
) -> None:
    frame, _ = inject(base, [crossed(base, 1500), price_spike(base, 2500)])
    data = build(frame, context)
    before = data.ticks.copy()
    for check_id in TICK_CHECKS:
        run(check_id, data, cfg)
    pd.testing.assert_frame_equal(data.ticks, before)


def test_tick_rate_ignores_hours_beyond_the_data(
    base: pd.DataFrame, context: dict[str, Any], cfg: AppConfig
) -> None:
    # The source's data ends at 10:00 UTC this day: later hours are missing data, not quiet hours.
    cut = int(pd.Timestamp("2024-03-12 10:00", tz="UTC").value)
    early = base[base["ts_utc"] < cut]
    coverage = (int(base["ts_utc"].min()), cut - 1)
    result = run("tick.rate_anomalies", build(early, context, coverage=coverage), cfg)
    assert result.metric == 0
    assert result.details["hours_compared"] == 11  # 23:00 .. 09:00 UTC, whole hours only
