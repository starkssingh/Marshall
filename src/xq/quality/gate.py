"""Quality gate for datasets (DQ-007).

A dataset may only contain trading days (partitions) that a quality run graded and did not FAIL.
`gate_partitions` checks every trading day a dataset would read against the results of one quality
run:

- a day with any FAIL result is refused unless the dataset spec excludes it explicitly, with a
  reason; the exclusion is recorded in the dataset manifest together with the failing checks;
- a day without results in the run is refused (it was never validated);
- a day with WARN results is included and listed, with its warning checks, in the manifest;
- a day may also be excluded voluntarily (for example a known bad export); it is recorded the same
  way;
- the source-wide exclusion list (``config/exclusions.yaml``, ADR 0071) is checked against the
  run's evidence by `check_exclusion_evidence` before its days are excluded.

The gate never alters data or thresholds; it only decides which partitions a dataset may use.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Engine, select

from xq.core.config import ExclusionRule
from xq.core.errors import XQError
from xq.quality.registry import Status
from xq.tracking.db import session_factory
from xq.tracking.models import QualityResultRecord, QualityRunRecord


class QualityGateError(XQError):
    """A dataset would include FAIL or unvalidated partitions."""


@dataclass(frozen=True)
class Exclusion:
    """A partition left out of a dataset, why, and which checks failed on it (if any)."""

    trading_day: date
    reason: str
    failing_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateDecision:
    """Which partitions a dataset may use, according to one quality run."""

    run_id: str
    included: tuple[date, ...]
    excluded: tuple[Exclusion, ...]
    warnings: Mapping[date, tuple[str, ...]] = field(default_factory=dict)

    def manifest_entry(self) -> dict[str, Any]:
        """The gate decision as recorded in a dataset manifest."""
        return {
            "quality_run_id": self.run_id,
            "included_partitions": len(self.included),
            "warn_partitions": [
                {"trading_day": day.isoformat(), "checks": list(checks)}
                for day, checks in sorted(self.warnings.items())
            ],
            "excluded_partitions": [
                {
                    "trading_day": e.trading_day.isoformat(),
                    "reason": e.reason,
                    "failing_checks": list(e.failing_checks),
                }
                for e in self.excluded
            ],
        }


def gate_partitions(
    engine: Engine,
    run_id: str,
    days: Iterable[date],
    exclusions: Mapping[date, str],
) -> GateDecision:
    """Decide which of `days` a dataset may use, given quality run `run_id`.

    Args:
        engine: Metadata database holding the quality run.
        run_id: The quality run the dataset is gated on.
        days: Every trading day the dataset would read (warm-up and context included).
        exclusions: Trading days the spec excludes explicitly, with reasons.

    Raises:
        QualityGateError: if a FAIL day is not excluded or a day has no results; the message
            names every such day and its failing checks.
    """
    wanted = sorted(set(days))
    with session_factory(engine)() as session:
        if session.get(QualityRunRecord, run_id) is None:
            raise QualityGateError(f"quality run {run_id} not found")
        rows = session.execute(
            select(
                QualityResultRecord.trading_day,
                QualityResultRecord.check_id,
                QualityResultRecord.status,
            ).where(QualityResultRecord.run_id == run_id)
        ).all()
    statuses: dict[date, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for day, check_id, status in rows:
        statuses[day][status].append(check_id)

    included: list[date] = []
    excluded: list[Exclusion] = []
    warnings: dict[date, tuple[str, ...]] = {}
    failing: list[str] = []
    unvalidated: list[date] = []
    for day in wanted:
        by_status = statuses.get(day)
        fails = tuple(sorted(by_status[Status.FAIL.value])) if by_status else ()
        if day in exclusions:
            excluded.append(Exclusion(day, exclusions[day], fails))
            continue
        if by_status is None:
            unvalidated.append(day)
            continue
        if fails:
            failing.append(f"{day} ({', '.join(fails)})")
            continue
        warns = tuple(sorted(by_status[Status.WARN.value]))
        if warns:
            warnings[day] = warns
        included.append(day)

    problems = []
    if failing:
        problems.append(f"FAIL partitions: {'; '.join(failing)}")
    if unvalidated:
        problems.append(
            f"partitions without results in quality run {run_id}: "
            f"{', '.join(d.isoformat() for d in unvalidated)}"
        )
    if problems:
        raise QualityGateError(
            "the quality gate refuses this dataset — "
            + " | ".join(problems)
            + ". Exclude a partition explicitly in the spec (exclusions: trading_day + reason) "
            "or re-run `xq validate` over the window"
        )
    return GateDecision(run_id, tuple(included), tuple(excluded), warnings)


def check_exclusion_evidence(
    engine: Engine, run_id: str, days: Iterable[date], rule: ExclusionRule
) -> None:
    """Refuse listed exclusions that quality run `run_id` contradicts (ADR 0071).

    A day on the exclusion list may be excluded only if more than the rule's share of its calendar
    market minutes is missing. Where the run graded the rule's check on the day, its metric must
    exceed that share; a day the run did not grade (no data at all) is not contradicted.

    Raises:
        QualityGateError: naming every listed day whose metric is at or below the rule.
    """
    wanted = sorted(set(days))
    with session_factory(engine)() as session:
        rows = session.execute(
            select(QualityResultRecord.trading_day, QualityResultRecord.metric_value).where(
                QualityResultRecord.run_id == run_id,
                QualityResultRecord.check_id == rule.check,
                QualityResultRecord.trading_day.in_(wanted),
            )
        ).all()
    limit = rule.max_missing_market_share
    contradicted = [f"{day} ({metric:.1%})" for day, metric in sorted(rows) if not metric > limit]
    if contradicted:
        raise QualityGateError(
            f"the exclusion list names days that quality run {run_id} does not support: "
            f"{', '.join(contradicted)} of market minutes missing, not more than {limit:.0%} "
            f"({rule.check}); remove them from config/exclusions.yaml"
        )
