"""Walk-forward report: per-fold metrics, the fold Sharpe distribution and a decay regression
(WF-005).

The plan's research rule is that fold-level results are always reported, never only the stitched
aggregate. Given a strategy's daily net returns on its out-of-sample days and the walk-forward
folds they came from, `walk_forward_report` gives:

- **per fold**: its training cutoff and test window, the trading days it holds, the mean daily
  net return, the annualized Sharpe ratio (BT-003's `return_metrics`), the summed net return, the
  share of positive days, and any metrics the fold recorded (the runner's forecast metrics);
- **the fold Sharpe distribution** over the folds with a defined Sharpe ratio: count, mean,
  median, standard deviation, quartiles, minimum, maximum and the share of positive ones, with the
  number of folds whose Sharpe ratio is undefined (no variance: a flat strategy, or one day);
- **the decay regression** (`xq.validation.decay.decay_trend`, the test R2's ``decay_trend``
  gate reads): each fold's mean daily return on its midpoint in years, with the slope per year,
  its t-statistic and the one-sided p-value of a falling edge; it needs at least four folds and
  is reported as not computed otherwise. It is descriptive here: no gate is judged and no trial
  is recorded.

A trading day belongs to the fold whose test window contains the day's start (17:00 New York the
evening before); a day whose start precedes every window (the first test day, when the first
window opens inside a trading day) belongs to the window holding its end. `board_report` builds
the report of one strategy of a recorded baseline board run from its stored fold-aligned returns
(``returns.parquet``) and the fold windows its walk-forward evaluations recorded.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine

from xq.backtest.metrics import return_metrics
from xq.core.time import trading_day_bounds
from xq.validation.decay import MIN_FOLDS, DecayTrend, decay_trend

#: Columns of the per-fold table.
FOLD_COLUMNS = (
    "fold_id",
    "train_end",
    "test_start",
    "test_end",
    "days",
    "mean_daily_bps",
    "sharpe",
    "net_return",
    "positive_days",
)


@dataclass(frozen=True)
class FoldWindow:
    """One walk-forward fold: its training cutoff, test window and recorded metrics."""

    fold_id: str
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    metrics: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class WalkForwardReport:
    """The report of one subject (module docstring)."""

    subject: str
    folds: pd.DataFrame
    distribution: dict[str, float]
    decay: DecayTrend | None
    decay_note: str | None
    periods_per_year: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready dictionary (non-finite numbers become null)."""
        return {
            "subject": self.subject,
            "note": self.note,
            "periods_per_year": self.periods_per_year,
            "folds": [
                {k: _jsonable(v) for k, v in row.items()}
                for row in self.folds.to_dict(orient="records")
            ],
            "fold_sharpe": {k: _jsonable(v) for k, v in self.distribution.items()},
            "decay": None
            if self.decay is None
            else {
                "slope_per_year": _jsonable(self.decay.slope_per_year),
                "t_stat": _jsonable(self.decay.t_stat),
                "p_value": _jsonable(self.decay.p_value),
                "n_folds": self.decay.n_folds,
            },
            "decay_note": self.decay_note,
        }

    def to_markdown(self) -> str:
        """The report as Markdown."""
        lines = [f"# Walk-forward report: {self.subject}", ""]
        if self.note:
            lines += [self.note, ""]
        lines += [
            "Fold-level results come first; the stitched series is never the only result (WF-005).",
            "",
            "## Folds",
            "",
            "| Fold | Train end | Test start | Test end | Days | Mean (bp/day) | Sharpe | "
            "Net return | Positive days |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        records: list[dict[str, Any]] = [
            {str(k): v for k, v in r.items()} for r in self.folds.to_dict(orient="records")
        ]
        for row in records:
            lines.append(
                f"| {row['fold_id']} | {_ts(row['train_end'])} | {_ts(row['test_start'])} | "
                f"{_ts(row['test_end'])} | {row['days']} | {_f(row['mean_daily_bps'])} | "
                f"{_f(row['sharpe'])} | {_pct(row['net_return'])} | {_pct(row['positive_days'])} |"
            )
        extra = [c for c in self.folds.columns if c not in FOLD_COLUMNS]
        if extra:
            lines += ["", "Recorded fold metrics:", ""]
            lines += ["| Fold | " + " | ".join(extra) + " |"]
            lines += ["| --- |" + " ---: |" * len(extra)]
            for row in records:
                values = " | ".join(_f(row[c]) for c in extra)
                lines.append(f"| {row['fold_id']} | {values} |")
        d = self.distribution
        lines += [
            "",
            "## Fold Sharpe distribution",
            "",
            f"{int(d['n_folds'])} folds: mean {_f(d['mean'])}, median {_f(d['median'])}, "
            f"standard deviation {_f(d['std'])}, quartiles {_f(d['q25'])} / {_f(d['q75'])}, "
            f"range {_f(d['min'])} to {_f(d['max'])}; {_pct(d['positive_share'])} of folds "
            "have a positive Sharpe ratio"
            + (
                f"; {int(d['undefined'])} folds have no defined Sharpe ratio (flat or one day)."
                if d["undefined"]
                else "."
            ),
            "",
            "## Decay regression",
            "",
        ]
        if self.decay is None:
            lines.append(f"Not computed: {self.decay_note}.")
        else:
            lines.append(
                "Fold mean daily net return regressed on the fold midpoint in years: slope "
                f"{_f(self.decay.slope_per_year * 1e4)} bp/day per year, t = "
                f"{_f(self.decay.t_stat)}, one-sided p = {_f(self.decay.p_value, 4)} for a "
                f"falling edge ({self.decay.n_folds} folds). Descriptive: no gate is judged here."
            )
        return "\n".join(lines) + "\n"


def walk_forward_report(
    subject: str,
    returns: pd.Series,
    folds: Sequence[FoldWindow],
    *,
    periods_per_year: int,
    note: str = "",
) -> WalkForwardReport:
    """The walk-forward report of daily net `returns` (indexed by trading day) over `folds`.

    Raises:
        ValueError: if a return is missing, no fold is given, or a day falls in no fold.
    """
    if not folds:
        raise ValueError("a walk-forward report needs at least one fold")
    days = [d if isinstance(d, date) else date.fromisoformat(str(d)) for d in returns.index]
    r = returns.to_numpy(np.float64)
    if not np.isfinite(r).all():
        raise ValueError("returns must not contain missing values")
    labels = assign_days(days, folds)
    missing = [d for d, label in zip(days, labels, strict=True) if label is None]
    if missing:
        raise ValueError(f"trading day {missing[0]} lies in no fold's test window")
    frame = pd.DataFrame({"r": r, "fold": labels})
    rows = []
    for fold in folds:
        part = frame.loc[frame["fold"] == fold.fold_id, "r"]
        metrics = return_metrics(part.reset_index(drop=True), periods_per_year)
        rows.append(
            {
                "fold_id": fold.fold_id,
                "train_end": fold.train_end,
                "test_start": fold.test_start,
                "test_end": fold.test_end,
                "days": len(part),
                "mean_daily_bps": float(part.mean()) * 1e4 if len(part) else math.nan,
                "sharpe": metrics["sharpe"],
                "net_return": float(part.sum()),
                "positive_days": float((part > 0).mean()) if len(part) else math.nan,
                **{k: float(v) for k, v in fold.metrics.items()},
            }
        )
    table = pd.DataFrame(rows)
    table = table.loc[table["days"] > 0].reset_index(drop=True)  # windows without OOS days
    sharpe = table["sharpe"].to_numpy(np.float64)
    finite = sharpe[np.isfinite(sharpe)]
    distribution = {
        "n_folds": float(len(finite)),
        "undefined": float(len(sharpe) - len(finite)),  # no variance, or a single day
        "mean": _stat(np.mean, finite),
        "median": _stat(np.median, finite),
        "std": float(np.std(finite, ddof=1)) if len(finite) > 1 else math.nan,
        "q25": _stat(lambda x: np.quantile(x, 0.25), finite),
        "q75": _stat(lambda x: np.quantile(x, 0.75), finite),
        "min": _stat(np.min, finite),
        "max": _stat(np.max, finite),
        "positive_share": float((finite > 0).mean()) if len(finite) else math.nan,
    }
    decay: DecayTrend | None = None
    decay_note = None
    if len(table) >= MIN_FOLDS:
        decay = decay_trend(r, np.asarray(labels), periods_per_year=periods_per_year)
    else:
        decay_note = f"{len(table)} folds with out-of-sample days; the regression needs {MIN_FOLDS}"
    return WalkForwardReport(
        subject, table, distribution, decay, decay_note, periods_per_year, note
    )


def assign_days(days: Sequence[date], folds: Sequence[FoldWindow]) -> list[str | None]:
    """The fold of each trading day (module docstring), or None when no window holds it."""
    starts = np.array([f.test_start.value for f in folds], dtype=np.int64)
    ends = np.array([f.test_end.value for f in folds], dtype=np.int64)
    out: list[str | None] = []
    for day in days:
        day_start, day_end = (b.value for b in trading_day_bounds(day))
        inside = np.flatnonzero((starts <= day_start) & (day_start < ends))
        if not len(inside):
            inside = np.flatnonzero((starts < day_end) & (day_end <= ends))
        out.append(folds[int(inside[0])].fold_id if len(inside) else None)
    return out


def board_report(
    engine: Engine, run_id: str, strategy: str, *, periods_per_year: int
) -> WalkForwardReport:
    """The walk-forward report of `strategy` in baseline board run `run_id` (module docstring).

    A forecast-sign strategy is named after its walk-forward evaluation (``<baseline>:<target>``)
    and carries that evaluation's fold metrics; a rule carries none (every evaluation of the run
    shares the board's folds).

    Raises:
        KeyError: if the run has no such strategy, no returns artifact or no fold results.
    """
    from xq.backtest.costs import SCREENING_LABEL
    from xq.models.board import RETURNS_ARTIFACT
    from xq.tracking import registry

    paths = [a.path for a in registry.list_artifacts(engine, run_id) if a.kind == RETURNS_ARTIFACT]
    if not paths:
        raise KeyError(f"run {run_id} has no {RETURNS_ARTIFACT} artifact (not a board run?)")
    returns = pd.read_parquet(paths[0])
    if strategy not in returns.columns:
        known = ", ".join(sorted(returns.columns))
        raise KeyError(f"run {run_id} has no strategy {strategy!r}; available: {known}")
    rows = registry.get_fold_results(engine, run_id)
    if not rows:
        raise KeyError(f"run {run_id} recorded no walk-forward folds")
    own = [r for r in rows if r.evaluation == strategy]  # a forecast-sign strategy's own folds
    windows: dict[str, FoldWindow] = {}
    for row in own or rows:
        if row.fold_id not in windows:
            metrics = {k: v for k, v in row.metrics.items() if own and v is not None}
            windows[row.fold_id] = FoldWindow(
                row.fold_id, row.train_end, row.test_start, row.test_end, metrics
            )
    note = (
        f"Baseline board run `{run_id}`, strategy `{strategy}`: its fold-aligned daily net "
        f"returns ({SCREENING_LABEL}; synthetic data unless the run says otherwise)."
    )
    return walk_forward_report(
        strategy,
        returns[strategy],
        sorted(windows.values(), key=lambda f: f.test_start),
        periods_per_year=periods_per_year,
        note=note,
    )


def write_report(report: WalkForwardReport, directory: Path) -> tuple[Path, Path]:
    """Write ``<subject>.md`` and ``<subject>.json`` into `directory`; return both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = report.subject.replace("@", "_at_").replace(":", "_").replace("/", "_")
    md, js = directory / f"{stem}.md", directory / f"{stem}.json"
    md.write_text(report.to_markdown(), encoding="utf-8")
    js.write_text(json.dumps(report.to_dict(), indent=2, default=str) + "\n", encoding="utf-8")
    return md, js


def _stat(fn: Any, values: np.ndarray[Any, Any]) -> float:
    return float(fn(values)) if len(values) else math.nan


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float | np.floating):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def _f(value: Any, digits: int = 2) -> str:
    return (
        f"{value:.{digits}f}" if isinstance(value, int | float) and math.isfinite(value) else "n/a"
    )


def _pct(value: Any) -> str:
    return (
        f"{100 * value:.1f} %" if isinstance(value, int | float) and math.isfinite(value) else "n/a"
    )


def _ts(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
