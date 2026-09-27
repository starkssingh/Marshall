"""The EDA report of a dataset's discovery window (Phase 4; ``xq research eda --dataset <id>``).

`run_eda` runs inside an experiment run (EXP-003). It loads the dataset's bars on the discovery
window (`xq.research.eda.data`), computes the sections below and writes one report with
`xq.research.reports.ReportBuilder` under ``reports/eda/<dataset_id>/<run_id>/``:

- ``overview`` — the discovery window, the bars and returns per timeframe, the excluded days;
- ``distributions`` (EDA-002) and ``horizons`` (EDA-006), each with its tables and figures;
- ``admission.yaml`` — the horizon admission list (copied to ``config/horizons.yaml`` only by
  ``xq research admit-horizons``);
- ``run.json`` — the run id, experiment, confirmatory flag, git sha and seed (outside the
  deterministic files and the manifest).

Every file of the report is recorded as a run artifact; the admission ratios are also logged as
metrics.

Bootstrap seeds derive from the run's seed, so the same dataset, configuration, commit and seed
reproduce the same report files. Nothing is a trial: EDA evaluates no trading rule.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from xq.backtest.costs import CostModel
from xq.core.config import config_hash
from xq.core.seeds import derive_seed
from xq.core.time import TimestampLike
from xq.core.types import Timeframe
from xq.research.eda import distributions, horizons
from xq.research.eda.bootstrap import eda_block_length
from xq.research.eda.data import EdaInputs, bars_per_trading_day, load_eda_inputs
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
    admission: horizons.HorizonAdmission
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
    cost = CostModel.from_config(cfg, inputs.spec.instrument)
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
        "cost_basis": cost.result_label,
    }
    builder = ReportBuilder(TITLE, metadata=metadata)
    _overview(builder.section("overview", "Data and discovery window"), inputs, returns)
    _distributions(builder.section("distributions", "Return distributions (EDA-002)"), run, returns)
    admission = _horizons(
        builder.section("horizons", "Cost to volatility and horizon admission (EDA-006)"),
        run,
        returns,
        cost,
    )
    provenance = {
        "dataset_id": dataset_id,
        "discovery_window": inputs.window.as_dict(),
        "git_sha": run.run.git_sha,
        "eda_config_hash": metadata["eda_config_hash"],
    }
    builder.attach(horizons.ADMISSION_FILE, horizons.admission_yaml(admission, provenance).encode())

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
    for horizon, ratio in admission.ratios.items():
        if math.isfinite(ratio):
            run.log_metric(f"eda/horizons/{horizon}/cost_to_vol", ratio)
    return EdaResult(directory, inputs.window, admission, files)


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


def _distributions(
    section: Section, run: RunContext, returns: dict[Timeframe, pd.DataFrame]
) -> None:
    eda = run.cfg.eda_config()
    sessions = run.cfg.sessions_config()
    boot = eda.bootstrap
    section.text(
        "Log returns in basis points. Intervals: stationary bootstrap "
        f"({boot.n_boot} resamples, {boot.ci_level:.0%}; mean block the Politis-White length of "
        f"squared returns, at least {boot.min_block_days} trading days of bars). Skewness and "
        "kurtosis are moment estimators (kurtosis in excess of the normal's). Hill indices use "
        f"the largest {eda.distributions.hill_tail_fraction:.0%} of each tail."
    )
    rows: dict[str, dict[str, float]] = {}
    yearly = []
    for tf in eda.timeframes:
        r = returns[tf]["ret"].to_numpy(dtype=np.float64)
        if len(r) < 3:
            continue
        block = eda_block_length(r, boot.min_block_days * bars_per_trading_day(tf, sessions))
        row = distributions.distribution_row(
            r,
            hill_fraction=eda.distributions.hill_tail_fraction,
            n_boot=boot.n_boot,
            mean_block=min(block, float(len(r))),
            level=boot.ci_level,
            seed=derive_seed(run.run.seed, "eda", "distributions", tf.value),
        )
        rows[tf.value] = row
        years = distributions.yearly_moments(returns[tf])
        years.insert(0, "timeframe", tf.value)
        yearly.append(years)
        t_params = (row["t_df"], row["t_loc"], row["t_scale"])
        section.figure(
            f"qq-{tf.value}",
            distributions.qq_figure(r, t_params, f"{tf.value} returns"),
            caption=f"QQ plots of {tf.value} returns against the fitted normal and Student-t",
        )
    summary = distributions.distribution_summary(rows)
    moment_columns = [
        "timeframe",
        "n",
        *(f"{m}{part}" for m in distributions.MOMENTS for part in ("", "_ci_low", "_ci_high")),
        "block_length",
    ]
    tail_columns = [c for c in summary.columns if c not in moment_columns or c == "timeframe"]
    section.table(
        "moments",
        summary[[c for c in moment_columns if c in summary.columns]],
        caption="Moments per timeframe with bootstrap intervals (bps)",
    )
    section.table(
        "tails",
        summary[tail_columns],
        caption="Normality, tail indices and the Student-t fit per timeframe (bps)",
    )
    if yearly:
        section.table(
            "by-year", pd.concat(yearly, ignore_index=True), caption="Moments per year (bps)"
        )


def _horizons(
    section: Section,
    run: RunContext,
    returns: dict[Timeframe, pd.DataFrame],
    cost: CostModel,
) -> horizons.HorizonAdmission:
    config = run.cfg.eda_config().horizons
    candidates = [tf.value for tf in config.candidates]
    table = horizons.cost_to_volatility_table(
        {tf.value: returns[tf] for tf in config.candidates},
        returns[Timeframe.M1],
        cost,
        run.cfg.sessions_config(),
        max_cost_to_vol=config.max_cost_to_vol,
        sigma_minutes=config.sigma_1m_minutes,
    )
    admission = horizons.admission(table, candidates, config.max_cost_to_vol)
    section.text(
        f"Costs: {cost.result_label}. Round-trip cost (spread, commission, slippage, financing) "
        "over the mean absolute log return of each horizon, overall and per session. Horizons "
        f"above {config.max_cost_to_vol:g} are excluded from directional research. "
        f"Admitted: {', '.join(admission.admitted) or 'none'}; "
        f"excluded: {', '.join(admission.excluded) or 'none'}."
    )
    section.table("cost-to-vol", table, caption=f"Cost to volatility ({cost.result_label})")
    if len(table):
        section.figure(
            "cost-to-vol",
            horizons.cost_to_volatility_figure(
                table, config.max_cost_to_vol, f"Cost to volatility ({cost.result_label})"
            ),
            caption="Overall cost-to-volatility ratio per horizon against the admission bound",
        )
    return admission


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
