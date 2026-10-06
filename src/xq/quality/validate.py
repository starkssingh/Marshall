"""`xq validate`: run every quality check over a source's trading days (DQ-006).

For each trading day the runner assembles a `PartitionData` from the pipeline stores — clean ticks
of the configured rules version, 1-minute bars of the configured build, the session-table row,
spread statistics (DATA-009), tick-rate norms, the counts of ticks cleaning dropped, and the
source's data coverage — grades every registered check, stores the results in ``quality_runs`` and
``quality_results`` and writes a report to ``reports/quality/<run_id>/``.

Validation is a pipeline stage, not a research read (ADR 0010). By default it covers only days
before ``vault.start``. Vault-period validation belongs to the release gate (GATE-002, ADR 0013):
``include_vault`` also needs ``vault_access_confirmed``, every confirmed use logs a
``vault_validation_access`` warning, and the run is recorded with ``includes_vault``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, func, select

from xq.core.config import AppConfig, config_hash
from xq.core.errors import VaultAccessError, XQError
from xq.core.logging import get_logger
from xq.core.time import to_ns, trading_day_bounds, utc_now
from xq.core.types import Timeframe
from xq.data.bars import bar_set_dir, build_version
from xq.data.clean import clean_partition_path, clean_rules_version
from xq.data.raw_store import active_raw_files
from xq.data.sessions import build_session_table
from xq.data.spreads import NoSpreadDataError, hour_of_week, latest_spread_stats
from xq.quality.checks.ticks import hourly_tick_counts
from xq.quality.registry import (
    CheckResult,
    PartitionData,
    Status,
    evaluate,
    load_builtin_checks,
    validate_thresholds,
)
from xq.quality.report import write_report
from xq.tracking.db import session_factory
from xq.tracking.models import (
    CleaningAction,
    CleanPartition,
    QualityResultRecord,
    QualityRunRecord,
)

QUALITY_DIR = "quality"
_MINUTE_NS = 60 * 1_000_000_000
_DEFAULT_MIN_WEEKS = 4

log = get_logger(__name__)


class NoQualityDataError(XQError):
    """There is nothing to validate: no clean partitions or no bars in the requested window."""


@dataclass
class ValidationResult:
    """What one `validate_source` call did."""

    run_id: str
    days: list[date]
    results: list[CheckResult]
    report_path: Path
    summary: dict[str, Any] = field(default_factory=dict)


def validate_source(
    cfg: AppConfig,
    engine: Engine,
    source_id: str,
    *,
    run_id: str,
    git_sha: str,
    start: date | None = None,
    end: date | None = None,
    include_vault: bool = False,
    vault_access_confirmed: bool = False,
) -> ValidationResult:
    """Grade every registered check on every selected trading day of `source_id`.

    Args:
        include_vault: Also validate trading days that end after ``vault.start``. Reserved for the
            release-gate procedure (ADR 0013); requires `vault_access_confirmed`.
        vault_access_confirmed: The caller's explicit acknowledgement that vault days will be
            read (``--i-understand-vault-access`` on the CLI).

    Raises:
        VaultAccessError: `include_vault` without `vault_access_confirmed`.
    """
    if include_vault and not vault_access_confirmed:
        raise VaultAccessError(
            "validating vault days is part of the release-gate procedure (GATE-002, ADR 0013); "
            "pass --i-understand-vault-access together with --include-vault to confirm"
        )
    quality = cfg.quality_config()
    registry = load_builtin_checks()
    validate_thresholds(registry, quality)
    source = cfg.source(source_id)
    clean_version = clean_rules_version(cfg)
    bars_version = build_version(cfg.bars_config(), clean_version)
    vault_ns = to_ns(pd.Timestamp(cfg.vault.start))

    with session_factory(engine)() as session:
        days = sorted(
            session.scalars(
                select(CleanPartition.trading_day).where(
                    CleanPartition.source_id == source_id,
                    CleanPartition.rules_version == clean_version,
                )
            )
        )
        dropped_rows = session.execute(
            select(CleanPartition.trading_day, CleaningAction.rule_id, func.count())
            .join(CleaningAction, CleaningAction.partition_id == CleanPartition.partition_id)
            .where(
                CleanPartition.source_id == source_id,
                CleanPartition.rules_version == clean_version,
                CleaningAction.action == "drop",
            )
            .group_by(CleanPartition.trading_day, CleaningAction.rule_id)
        ).all()
        spans = [
            (to_ns(r.first_ts_utc), to_ns(r.last_ts_utc))
            for r in active_raw_files(session, source_id)
            if r.first_ts_utc is not None and r.last_ts_utc is not None
        ]
    days = [d for d in days if (start is None or d >= start) and (end is None or d <= end)]
    vault_days = [d for d in days if to_ns(trading_day_bounds(d)[1]) > vault_ns]
    if not include_vault:
        days = [d for d in days if d not in vault_days]
    elif vault_days:
        log.warning(
            "vault_validation_access",
            run_id=run_id,
            source_id=source_id,
            vault_start=str(cfg.vault.start),
            vault_days=[d.isoformat() for d in vault_days],
        )
    if not days:
        raise NoQualityDataError(
            f"no clean partitions of {source_id!r} to validate in the requested window "
            "(before the vault unless --include-vault); run `xq clean` first"
        )
    bars_root = bar_set_dir(cfg, source_id, Timeframe.M1, bars_version)
    if not bars_root.is_dir():
        raise NoQualityDataError(
            f"no 1-minute bars for build {bars_version}; run `xq build-bars` first"
        )

    dropped: dict[date, dict[str, int]] = defaultdict(dict)
    for day, rule, count in dropped_rows:
        dropped[day][rule] = int(count)
    coverage = (min(s[0] for s in spans), max(s[1] for s in spans))
    table = build_session_table(cfg.sessions_config(), days[0], days[-1]).set_index("trading_day")
    try:
        spread_stats: pd.DataFrame | None = latest_spread_stats(engine, source_id)
    except NoSpreadDataError:
        spread_stats = None

    def ticks_of(day: date, columns: list[str] | None = None) -> pd.DataFrame:
        path = clean_partition_path(cfg, source_id, day, clean_version)
        return pd.read_parquet(path, columns=columns)

    min_weeks = int(
        quality.checks["tick.rate_anomalies"].params.get("min_weeks", _DEFAULT_MIN_WEEKS)
    )
    norm = _tick_rate_norm(days, table, ticks_of, min_weeks, coverage)
    month_bars: dict[str, pd.DataFrame] = {}
    results: list[CheckResult] = []
    missing_rows: list[pd.DataFrame] = []

    for day in days:
        row = _day_row(table, day)
        month = f"year={day.year:04d}/month={day.month:02d}"
        if month not in month_bars:
            path = bars_root / month / "part.parquet"
            month_bars[month] = pd.read_parquet(path) if path.is_file() else pd.DataFrame()
        bars = month_bars[month]
        day_bars = (
            bars[bars["trading_day"] == day].sort_values("bar_start_utc", ignore_index=True)
            if not bars.empty
            else bars
        )
        data = PartitionData(
            source_id=source_id,
            instrument_id=source.instrument,
            trading_day=day,
            ticks=ticks_of(day).sort_values("ts_utc", kind="stable", ignore_index=True),
            bars_1m=day_bars,
            day=row,
            spread_stats=spread_stats,
            hourly_tick_norm=norm,
            dropped=dropped.get(day, {}),
            coverage=coverage,
        )
        results.extend(r for c in registry.checks() if (r := evaluate(c, data, quality)))
        missing_rows.append(_missing_minutes(data, coverage))

    report_dir = cfg.paths.resolve(cfg.paths.reports_dir) / QUALITY_DIR / run_id
    first_start = trading_day_bounds(days[0])[0]
    last_end = trading_day_bounds(days[-1])[1]
    meta = {
        "run_id": run_id,
        "source_id": source_id,
        "instrument_id": source.instrument,
        "trading_days": f"{days[0]} to {days[-1]} ({len(days)} days)",
        "window_utc": f"{first_start} to {last_end}",
        "clean_rules_version": clean_version,
        "bar_build_version": bars_version,
        "includes_vault": include_vault,
        "git_sha": git_sha,
        "config_hash": config_hash(cfg),
        "spread_statistics": "available" if spread_stats is not None else "missing",
        "tick_rate_norm_hours": len(norm) if norm is not None else 0,
    }
    missing = pd.concat(missing_rows, ignore_index=True)
    report_path, summary = write_report(
        report_dir,
        meta=meta,
        results=results,
        quality=quality,
        descriptions={c.check_id: c.description for c in registry.checks()},
        missing_minutes=missing,
        spread_stats=spread_stats,
    )

    with session_factory(engine)() as session:
        session.add(
            QualityRunRecord(
                run_id=run_id,
                scope="trading_day",
                source_id=source_id,
                start_utc=first_start,
                end_utc=last_end,
                rules_version=clean_version,
                build_version=bars_version,
                includes_vault=include_vault,
                git_sha=git_sha,
                config_hash=config_hash(cfg),
                report_path=str(report_path),
                summary_json=summary,
                created_at=utc_now(),
            )
        )
        session.flush()
        session.add_all(
            QualityResultRecord(
                run_id=run_id,
                partition_id=r.partition_id,
                check_id=r.check_id,
                trading_day=r.trading_day,
                severity=r.severity,
                metric_value=r.metric,
                warn_threshold=r.warn,
                fail_threshold=r.fail,
                status=r.status.value,
                details_json=_json_safe(
                    {
                        **r.details,
                        "anomalies": [
                            {"ts_utc": str(a.ts_utc), "value": a.value, "note": a.note}
                            for a in r.anomalies
                        ],
                    }
                ),
            )
            for r in results
        )
        session.commit()
    log.info(
        "quality_run_finished",
        days=len(days),
        **{status.value: sum(r.status is status for r in results) for status in Status},
    )
    return ValidationResult(run_id, days, results, report_path, summary)


def _tick_rate_norm(
    days: list[date],
    table: pd.DataFrame,
    ticks_of: Any,
    min_weeks: int,
    coverage: tuple[int, int],
) -> pd.Series | None:
    """Median ticks per New York hour of week, for hours seen in at least `min_weeks` weeks.

    Only hours inside the source's data coverage count: the hours after an export ends are
    missing data, not quiet markets.
    """
    counts = []
    for day in days:
        row = _day_row(table, day)
        if not row["is_open"]:
            continue
        opens = max(to_ns(pd.Timestamp(row["market_open_utc"])), coverage[0])
        closes = min(to_ns(pd.Timestamp(row["market_close_utc"])), coverage[1] + 1)
        if closes <= opens:
            continue
        ts = ticks_of(day, ["ts_utc"])["ts_utc"].to_numpy(dtype=np.int64)
        counts.append(hourly_tick_counts(ts, [(opens, closes)]))
    if not counts:
        return None
    observed = pd.concat(counts)
    grouped = observed.groupby(level=0)
    norm = grouped.median()[grouped.size() >= min_weeks]
    return norm if not norm.empty else None


def _day_row(table: pd.DataFrame, day: date) -> pd.Series:
    """The session-table row of `day` (the table is indexed by trading day)."""
    row: pd.Series = table.loc[[day]].iloc[0]
    return row


def _missing_minutes(data: PartitionData, coverage: tuple[int, int]) -> pd.DataFrame:
    """Expected and missing market-hours minutes of one day by (week, New York hour of week)."""
    columns = ["week", "hour_of_week", "expected", "missing"]
    if not data.day["is_open"]:
        return pd.DataFrame(columns=columns)
    opens = max(to_ns(pd.Timestamp(data.day["market_open_utc"])), coverage[0])
    closes = min(to_ns(pd.Timestamp(data.day["market_close_utc"])), coverage[1] + 1)
    minutes = np.arange(
        -(-opens // _MINUTE_NS) * _MINUTE_NS, closes - _MINUTE_NS + 1, _MINUTE_NS, dtype=np.int64
    )
    if len(minutes) == 0:
        return pd.DataFrame(columns=columns)
    present: np.ndarray = (
        data.bars_1m["bar_start_utc"].to_numpy(dtype=np.int64)
        if len(data.bars_1m)
        else np.array([], dtype=np.int64)
    )
    frame = pd.DataFrame(
        {
            "hour_of_week": hour_of_week(minutes),
            "missing": ~np.isin(minutes, present),
        }
    )
    monday = data.trading_day - pd.Timedelta(days=data.trading_day.weekday())
    grouped = frame.groupby("hour_of_week")["missing"].agg(["size", "sum"]).reset_index()
    return pd.DataFrame(
        {
            "week": monday,
            "hour_of_week": grouped["hour_of_week"],
            "expected": grouped["size"],
            "missing": grouped["sum"],
        }
    )


def _json_safe(value: Any) -> Any:
    """Recursively convert numpy and pandas scalars so the value can be stored as JSON."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return str(value)
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value
