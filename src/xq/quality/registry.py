"""Data-quality check framework (DQ-001).

A check measures one aspect of one partition (a trading day of one source) and returns a
`Measurement`: a number where larger is worse, plus details and the worst anomalies. The framework
grades the number against thresholds from ``config/quality.yaml`` (FAIL above ``fail``, WARN above
``warn``, else PASS), so checks contain no thresholds and every threshold lives in configuration.
Checks only read data; nothing here changes a tick or a bar.

Checks register themselves in `REGISTRY` with the `register` decorator. `load_builtin_checks`
imports the built-in check modules (``xq.quality.checks``) so they are discoverable.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Protocol

import pandas as pd

from xq.core.config import CheckThreshold, QualityConfig
from xq.core.errors import ConfigError


class Status(StrEnum):
    """Grade of one check on one partition."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class Scope(StrEnum):
    """What a check looks at."""

    TICK = "tick"
    BAR = "bar"
    PARTITION = "partition"


@dataclass(frozen=True)
class Anomaly:
    """One noteworthy observation, for the report and the human review (DQ-008)."""

    ts_utc: pd.Timestamp
    value: float
    note: str = ""


@dataclass(frozen=True)
class Measurement:
    """What a check measured; larger `metric` is worse."""

    metric: float
    details: dict[str, Any] = field(default_factory=dict)
    anomalies: list[Anomaly] = field(default_factory=list)


@dataclass(frozen=True)
class PartitionData:
    """Everything a check may look at for one trading day of one source.

    Attributes:
        ticks: Clean ticks of the day (all rows, with flags), sorted by ``ts_utc``.
        bars_1m: 1-minute bars of the day (``BAR_SCHEMA``), sorted by ``bar_start_utc``.
        day: The trading day's row of the session table (`xq.data.sessions`).
        spread_stats: Hour-of-week spread percentiles (``hour_of_week``, ``p50``...), if any.
        hourly_tick_norm: Typical tick count per New York hour of week, if enough history exists.
        dropped: Ticks removed by cleaning, per rule id (only rules configured to drop).
    """

    source_id: str
    instrument_id: str
    trading_day: date
    ticks: pd.DataFrame
    bars_1m: pd.DataFrame
    day: pd.Series
    spread_stats: pd.DataFrame | None = None
    hourly_tick_norm: pd.Series | None = None
    dropped: Mapping[str, int] = field(default_factory=dict)

    @property
    def partition_id(self) -> str:
        return f"{self.source_id}:{self.instrument_id}:{self.trading_day.isoformat()}"


@dataclass(frozen=True)
class CheckResult:
    """A graded measurement, as stored in ``quality_results``."""

    check_id: str
    partition_id: str
    trading_day: date
    scope: Scope
    severity: str
    metric: float
    warn: float | None
    fail: float | None
    status: Status
    details: dict[str, Any]
    anomalies: list[Anomaly]


class Check(Protocol):
    """A data-quality check."""

    check_id: str
    scope: Scope
    description: str

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        """Measure one partition; return None when the check does not apply to it.

        `params` holds the check's configured parameters plus ``active_sessions``.
        """
        ...


class CheckRegistry:
    """Discoverable collection of checks keyed by id."""

    def __init__(self) -> None:
        self._checks: dict[str, Check] = {}

    def add(self, check: Check) -> None:
        if check.check_id in self._checks:
            raise ValueError(f"check {check.check_id!r} is already registered")
        self._checks[check.check_id] = check

    def get(self, check_id: str) -> Check:
        return self._checks[check_id]

    def ids(self) -> list[str]:
        return sorted(self._checks)

    def checks(self) -> list[Check]:
        return [self._checks[i] for i in self.ids()]


REGISTRY = CheckRegistry()


def register[C: Check](cls: type[C]) -> type[C]:
    """Class decorator: instantiate the check and add it to `REGISTRY`."""
    REGISTRY.add(cls())
    return cls


def load_builtin_checks() -> CheckRegistry:
    """Import every module in ``xq.quality.checks`` so its checks register; return the registry."""
    package = importlib.import_module("xq.quality.checks")
    for module in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{module.name}")
    return REGISTRY


def validate_thresholds(registry: CheckRegistry, cfg: QualityConfig) -> None:
    """Every registered check needs thresholds and every threshold needs a registered check."""
    missing = sorted(set(registry.ids()) - set(cfg.checks))
    unknown = sorted(set(cfg.checks) - set(registry.ids()))
    if missing or unknown:
        raise ConfigError(
            "quality thresholds do not match the registered checks: "
            f"missing thresholds for {missing}, thresholds for unknown checks {unknown}"
        )


def grade(metric: float, threshold: CheckThreshold) -> Status:
    """FAIL if metric > fail, else WARN if metric > warn, else PASS."""
    if threshold.fail is not None and metric > threshold.fail:
        return Status.FAIL
    if threshold.warn is not None and metric > threshold.warn:
        return Status.WARN
    return Status.PASS


def evaluate(check: Check, data: PartitionData, cfg: QualityConfig) -> CheckResult | None:
    """Measure and grade one check on one partition (None when it does not apply)."""
    threshold = cfg.checks[check.check_id]
    params = {"active_sessions": list(cfg.active_sessions), **threshold.params}
    measurement = check.measure(data, params)
    if measurement is None:
        return None
    anomalies = sorted(measurement.anomalies, key=lambda a: -abs(a.value))[: cfg.top_anomalies]
    return CheckResult(
        check_id=check.check_id,
        partition_id=data.partition_id,
        trading_day=data.trading_day,
        scope=check.scope,
        severity=threshold.severity,
        metric=float(measurement.metric),
        warn=threshold.warn,
        fail=threshold.fail,
        status=grade(float(measurement.metric), threshold),
        details=measurement.details,
        anomalies=anomalies,
    )


def run_checks(
    partitions: Iterable[PartitionData],
    cfg: QualityConfig,
    registry: CheckRegistry | None = None,
    *,
    on_partition: Callable[[PartitionData, list[CheckResult]], None] | None = None,
) -> list[CheckResult]:
    """Run every registered check on every partition; thresholds are validated first."""
    checks = registry if registry is not None else load_builtin_checks()
    validate_thresholds(checks, cfg)
    results: list[CheckResult] = []
    for data in partitions:
        partition_results = [r for c in checks.checks() if (r := evaluate(c, data, cfg))]
        if on_partition is not None:
            on_partition(data, partition_results)
        results.extend(partition_results)
    return results
