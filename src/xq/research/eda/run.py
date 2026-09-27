"""The EDA report of a dataset's discovery window (Phase 4; ``xq research eda --dataset <id>``).

`run_eda` runs inside an experiment run (EXP-003). It loads the dataset's bars on the discovery
window (`xq.research.eda.data`), computes the sections below and writes one report with
`xq.research.reports.ReportBuilder` under ``reports/eda/<dataset_id>/<run_id>/``:

- ``overview`` — the discovery window, the bars and returns per timeframe, the excluded days;
- ``run.json`` — the run id, experiment, confirmatory flag, git sha and seed (outside the
  deterministic files and the manifest).

Every file of the report is recorded as a run artifact. The same dataset, configuration, commit
and seed reproduce the same report files. Nothing is a trial: EDA evaluates no trading rule.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from xq.core.config import config_hash
from xq.core.time import TimestampLike
from xq.core.types import Timeframe
from xq.research.eda.data import EdaInputs, load_eda_inputs
from xq.research.reports import (
    MANIFEST_FILE,
    RUN_FILE,
    DiscoveryWindow,
    ReportBuilder,
    Section,
)

if TYPE_CHECKING:
    from xq.tracking.runs import RunContext

REPORT_DIR = "eda"
TITLE = "Exploratory research on the discovery window"


@dataclass(frozen=True)
class EdaResult:
    """What `run_eda` produced."""

    report_dir: Path
    window: DiscoveryWindow
    files: dict[str, str]


def run_eda(run: RunContext, dataset_id: str, *, end: TimestampLike | None = None) -> EdaResult:
    """Write the EDA report of `dataset_id` (see the module docstring).

    Args:
        end: Stop before the discovery window's end; a later instant is refused.

    Raises:
        DiscoveryWindowError: for data or an `end` outside the discovery window.
    """
    cfg = run.cfg
    eda = cfg.eda_config()
    wanted = [
        *eda.timeframes,
        *eda.horizons.candidates,
        eda.seasonality.intraday_timeframe,
        eda.seasonality.event_timeframe,
        eda.trend.variance_ratio_timeframe,
        eda.trend.run_timeframe,
        Timeframe.M1,
        Timeframe.D1,
    ]
    timeframes = sorted(set(wanted), key=lambda tf: tf.nanos)
    inputs = load_eda_inputs(cfg, dataset_id, timeframes, end=end)
    returns = {tf: inputs.returns(tf) for tf in timeframes}
    eda_json = eda.model_dump(mode="json")
    metadata: dict[str, Any] = {
        "dataset_id": dataset_id,
        "source": inputs.spec.source,
        "instrument": inputs.spec.instrument,
        "bar_build": inputs.spec.bar_build,
        "price_basis": str(inputs.spec.price_basis),
        "discovery_window": inputs.window.as_dict(),
        "excluded_trading_days": sorted(str(d) for d in inputs.excluded_days),
        "git_sha": run.run.git_sha,
        "uv_lock_sha256": run.run.lock_hash,
        "seed": run.run.seed,
        "app_config_hash": config_hash(cfg),
        "eda_config_hash": _hash(eda_json),
        "eda_config": eda_json,
    }
    builder = ReportBuilder(TITLE, metadata=metadata)
    _overview(builder.section("overview", "Data and discovery window"), inputs, returns)

    directory = cfg.paths.resolve(cfg.paths.reports_dir) / REPORT_DIR / dataset_id / run.run_id
    directory.mkdir(parents=True, exist_ok=False)
    files = builder.build(directory)
    record = {
        "run_id": run.run_id,
        "experiment_id": run.run.experiment_id,
        "confirmatory": run.confirmatory,
        "git_sha": run.run.git_sha,
        "seed": run.run.seed,
        "dataset_id": dataset_id,
    }
    (directory / RUN_FILE).write_text(json.dumps(record, indent=2) + "\n")
    for relative in [*sorted(files), MANIFEST_FILE, RUN_FILE]:
        run.log_artifact(directory / relative, kind="eda_report")
    return EdaResult(directory, inputs.window, files)


def _overview(section: Section, inputs: EdaInputs, returns: dict[Timeframe, pd.DataFrame]) -> None:
    window = inputs.window
    section.text(
        f"Discovery window: {window.start} to {window.end} ({window.rule}). Only bars that start "
        "inside it and are available by its end are read; the dataset's excluded trading days "
        f"({len(inputs.excluded_days)}) are left out. Returns are close-to-close log returns of "
        "bars adjacent in market time: returns across missing or excluded bars are dropped, "
        "returns across the daily break and weekends are kept."
    )
    rows = []
    for tf, frame in returns.items():
        bars = inputs.bars[tf]
        rows.append(
            {
                "timeframe": tf.value,
                "bars": len(bars),
                "returns": len(frame),
                "dropped": max(0, len(bars) - 1 - len(frame)),
                "first_bar": str(bars["bar_start_utc"].iloc[0]) if len(bars) else "n/a",
                "last_bar": str(bars["bar_start_utc"].iloc[-1]) if len(bars) else "n/a",
            }
        )
    section.table("coverage", pd.DataFrame(rows), caption="Bars and returns per timeframe")


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
