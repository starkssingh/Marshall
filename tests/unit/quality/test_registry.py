"""DQ-001: check framework, registry and thresholds."""

from collections.abc import Mapping
from datetime import date
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from helpers.quality import repo_config
from xq.core.config import CheckThreshold, QualityConfig
from xq.core.errors import ConfigError
from xq.quality.registry import (
    Anomaly,
    CheckRegistry,
    Measurement,
    PartitionData,
    Scope,
    Status,
    evaluate,
    grade,
    load_builtin_checks,
    run_checks,
    validate_thresholds,
)


class CountTicks:
    """A toy check: the metric is the number of ticks; not applicable to empty days."""

    check_id = "test.count"
    scope = Scope.TICK
    description = "number of ticks"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        if data.ticks.empty:
            return None
        anomalies = [Anomaly(pd.Timestamp(0, tz="UTC"), float(v)) for v in range(-30, 30)]
        return Measurement(
            len(data.ticks) * params.get("scale", 1), {"n": len(data.ticks)}, anomalies
        )


def data(n: int, day: date = date(2024, 3, 12)) -> PartitionData:
    ticks = pd.DataFrame({"ts_utc": range(n)})
    return PartitionData("src", "xauusd", day, ticks, pd.DataFrame(), pd.Series(dtype=object))


def config(**threshold: Any) -> QualityConfig:
    values = {"severity": "major", "unit": "count", "warn": 5, "fail": 10, **threshold}
    return QualityConfig(
        active_sessions=["london"],
        top_anomalies=3,
        checks={"test.count": CheckThreshold.model_validate(values)},
    )


def registry() -> CheckRegistry:
    checks = CheckRegistry()
    checks.add(CountTicks())
    return checks


@pytest.mark.parametrize(
    ("metric", "warn", "fail", "expected"),
    [
        (0.0, 0.0, 1.0, Status.PASS),  # "warn on any": zero passes
        (0.1, 0.0, 1.0, Status.WARN),
        (1.0, 0.0, 1.0, Status.WARN),  # at the fail threshold is not above it
        (1.1, 0.0, 1.0, Status.FAIL),
        (5.0, None, 0.0, Status.FAIL),  # no warn level
        (5.0, 1.0, None, Status.WARN),  # never fails
        (0.0, None, 0.0, Status.PASS),
    ],
)
def test_grading(metric: float, warn: float | None, fail: float | None, expected: Status) -> None:
    threshold = CheckThreshold(severity="minor", unit="x", warn=warn, fail=fail)
    assert grade(metric, threshold) is expected


def test_warn_above_fail_is_rejected() -> None:
    with pytest.raises(ValidationError, match="warn threshold"):
        CheckThreshold(severity="minor", unit="x", warn=2, fail=1)


def test_registry_rejects_duplicates_and_lists_ids() -> None:
    checks = registry()
    assert checks.ids() == ["test.count"]
    with pytest.raises(ValueError, match="already registered"):
        checks.add(CountTicks())


def test_thresholds_must_match_registered_checks() -> None:
    validate_thresholds(registry(), config())
    with pytest.raises(ConfigError, match=r"missing thresholds for \['test.count'\]"):
        validate_thresholds(registry(), QualityConfig(active_sessions=[], checks={}))
    with pytest.raises(ConfigError, match=r"unknown checks \['test.count'\]"):
        validate_thresholds(CheckRegistry(), config())


def test_evaluate_grades_and_keeps_the_worst_anomalies() -> None:
    result = evaluate(CountTicks(), data(7), config())
    assert result is not None
    assert result.status is Status.WARN
    assert result.metric == 7
    assert result.severity == "major"
    assert (result.warn, result.fail) == (5, 10)
    assert result.partition_id == "src:xauusd:2024-03-12"
    assert result.details == {"n": 7}
    # Top 3 by magnitude; equal magnitudes keep the order the check reported them in.
    assert [a.value for a in result.anomalies] == [-30.0, -29.0, 29.0]


def test_params_reach_the_check() -> None:
    result = evaluate(CountTicks(), data(7), config(params={"scale": 2}))
    assert result is not None
    assert result.metric == 14
    assert result.status is Status.FAIL


def test_not_applicable_gives_no_result() -> None:
    assert evaluate(CountTicks(), data(0), config()) is None


def test_run_checks_over_partitions() -> None:
    seen: list[str] = []
    results = run_checks(
        [data(3), data(0, date(2024, 3, 13)), data(12, date(2024, 3, 14))],
        config(),
        registry(),
        on_partition=lambda d, r: seen.append(f"{d.trading_day}:{len(r)}"),
    )
    assert [r.status for r in results] == [Status.PASS, Status.FAIL]
    assert seen == ["2024-03-12:1", "2024-03-13:0", "2024-03-14:1"]


def test_repository_thresholds_match_the_builtin_checks() -> None:
    validate_thresholds(load_builtin_checks(), repo_config().quality_config())
