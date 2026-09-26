"""Quality report: Markdown, JSON summary and heatmaps for one quality run (DQ-006).

Written to ``reports/quality/<run_id>/``:

- ``report.md`` — run metadata; a summary table (days PASS/WARN/FAIL and the worst day per check);
  days with failures; per-check metric statistics; the top anomalies per check with timestamps;
- ``summary.json`` — the same summary for machines (also stored in ``quality_runs``);
- ``missing_minutes.png`` — share of market-hours minutes without a 1-minute bar, week x New York
  hour of week;
- ``spreads.png`` — spread p50 and p90 by New York weekday and hour (when statistics exist).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from xq.core.config import QualityConfig
from xq.quality.registry import CheckResult, Status

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def summarise(results: list[CheckResult], quality: QualityConfig) -> dict[str, Any]:
    """Per-check counts of PASS/WARN/FAIL, worst metric and day, and overall totals."""
    checks: dict[str, Any] = {}
    for check_id in sorted(quality.checks):
        own = [r for r in results if r.check_id == check_id]
        counts = {status.value: sum(r.status is status for r in own) for status in Status}
        worst = max(own, key=lambda r: r.metric, default=None)
        checks[check_id] = {
            "severity": quality.checks[check_id].severity,
            "unit": quality.checks[check_id].unit,
            "warn_threshold": quality.checks[check_id].warn,
            "fail_threshold": quality.checks[check_id].fail,
            "days_evaluated": len(own),
            **counts,
            "worst_metric": worst.metric if worst else None,
            "worst_day": worst.trading_day.isoformat() if worst and worst.metric > 0 else None,
        }
    totals = {status.value: sum(r.status is status for r in results) for status in Status}
    failing_days = sorted({r.trading_day.isoformat() for r in results if r.status is Status.FAIL})
    return {"checks": checks, "totals": totals, "days_with_failures": failing_days}


def write_report(
    directory: Path,
    *,
    meta: Mapping[str, Any],
    results: list[CheckResult],
    quality: QualityConfig,
    descriptions: Mapping[str, str],
    missing_minutes: pd.DataFrame,
    spread_stats: pd.DataFrame | None,
) -> tuple[Path, dict[str, Any]]:
    """Write the report files; return the Markdown path and the summary."""
    directory.mkdir(parents=True, exist_ok=True)
    summary = summarise(results, quality)
    summary["meta"] = dict(meta)
    (directory / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")

    figures = []
    if _missing_heatmap(missing_minutes, directory / "missing_minutes.png"):
        figures.append(
            ("Missing market-hours minutes (week x New York hour of week)", "missing_minutes.png")
        )
    if spread_stats is not None and _spread_heatmap(spread_stats, directory / "spreads.png"):
        figures.append(("Spread p50 and p90 by New York weekday and hour", "spreads.png"))

    lines = [f"# Data-quality report {meta['run_id']}", ""]
    lines += [f"- **{key.replace('_', ' ')}:** {value}" for key, value in meta.items()]
    lines += ["", "## Summary", ""]
    lines += [
        f"Results: {summary['totals']['pass']} pass, {summary['totals']['warn']} warn, "
        f"{summary['totals']['fail']} fail. Grading: FAIL if metric > fail, WARN if metric > warn; "
        "a check with 0 days did not apply (e.g. no holiday, too little tick-rate history).",
        "",
        "| Check | Severity | Metric | Warn | Fail | Days | Pass | Warn | Fail | Worst | Worst day "
        "|",
        "|" + " --- |" * 11,
    ]
    for check_id, row in summary["checks"].items():
        cells = [
            f"`{check_id}`",
            row["severity"],
            row["unit"],
            _fmt(row["warn_threshold"]),
            _fmt(row["fail_threshold"]),
            str(row["days_evaluated"]),
            str(row["pass"]),
            str(row["warn"]),
            str(row["fail"]),
            _fmt(row["worst_metric"]),
            row["worst_day"] or "—",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "## Days with failures", ""]
    if summary["days_with_failures"]:
        for day in summary["days_with_failures"]:
            failing = sorted(
                r.check_id
                for r in results
                if r.trading_day.isoformat() == day and r.status is Status.FAIL
            )
            lines.append(f"- {day}: {', '.join(f'`{c}`' for c in failing)}")
    else:
        lines.append("None.")

    lines += ["", "## Per-check statistics", ""]
    lines += [
        "| Check | What it measures | Mean | Median | Max |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check_id in summary["checks"]:
        metrics = np.array([r.metric for r in results if r.check_id == check_id])
        stats = (
            (_fmt(metrics.mean()), _fmt(float(np.median(metrics))), _fmt(metrics.max()))
            if len(metrics)
            else ("—", "—", "—")
        )
        lines.append(f"| `{check_id}` | {descriptions.get(check_id, '')} | {' | '.join(stats)} |")

    lines += ["", f"## Top anomalies (up to {quality.top_anomalies} per check)", ""]
    for check_id in summary["checks"]:
        anomalies = [
            (r.trading_day, a) for r in results if r.check_id == check_id for a in r.anomalies
        ]
        if not anomalies:
            continue
        anomalies.sort(key=lambda item: -abs(item[1].value))
        lines += [
            f"### `{check_id}`",
            "",
            "| Trading day | Time (UTC) | Value | Note |",
            "| --- | --- | --- | --- |",
        ]
        for day, anomaly in anomalies[: quality.top_anomalies]:
            lines.append(f"| {day} | {anomaly.ts_utc} | {_fmt(anomaly.value)} | {anomaly.note} |")
        lines.append("")

    if figures:
        lines += ["## Figures", ""]
        for title, name in figures:
            lines += [f"### {title}", "", f"![{title}]({name})", ""]

    path = directory / "report.md"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path, summary


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.4g}"


def _missing_heatmap(missing: pd.DataFrame, path: Path) -> bool:
    if missing.empty:
        return False
    grouped = missing.groupby(["week", "hour_of_week"])[["expected", "missing"]].sum()
    share = (grouped["missing"] / grouped["expected"]).unstack("hour_of_week")
    share = share.reindex(columns=range(168))
    figure, axis = plt.subplots(figsize=(14, max(2.0, 0.3 * len(share) + 1)))
    image = axis.imshow(
        share.to_numpy(dtype=float),
        aspect="auto",
        cmap="Reds",
        vmin=0,
        vmax=1,
        interpolation="nearest",
    )
    axis.set_xticks(range(0, 168, 24), WEEKDAYS)
    axis.set_yticks(range(len(share)), [str(w) for w in share.index])
    axis.set_xlabel("New York hour of week (blank = market closed)")
    axis.set_ylabel("Week starting")
    figure.colorbar(image, ax=axis, label="missing share")
    figure.tight_layout()
    figure.savefig(path, dpi=100)
    plt.close(figure)
    return True


def _spread_heatmap(stats: pd.DataFrame, path: Path) -> bool:
    if stats.empty:
        return False
    figure, axes = plt.subplots(1, 2, figsize=(14, 3.5))
    for axis, column in zip(axes, ("p50", "p90"), strict=True):
        grid = np.full((7, 24), np.nan)
        for hour, value in zip(stats["hour_of_week"], stats[column], strict=True):
            grid[int(hour) // 24, int(hour) % 24] = value
        image = axis.imshow(grid, aspect="auto", cmap="viridis", interpolation="nearest")
        axis.set_yticks(range(7), WEEKDAYS)
        axis.set_xticks(range(0, 24, 3))
        axis.set_xlabel("New York hour")
        axis.set_title(f"spread {column}")
        figure.colorbar(image, ax=axis)
    figure.tight_layout()
    figure.savefig(path, dpi=100)
    plt.close(figure)
    return True
