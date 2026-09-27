"""The EDA report of a dataset's discovery window (Phase 4; ``xq research eda --dataset <id>``).

`run_eda` runs inside an experiment run (EXP-003). It loads the dataset's bars on the discovery
window (`xq.research.eda.data`), computes the sections below and writes one report with
`xq.research.reports.ReportBuilder` under ``reports/eda/<dataset_id>/<run_id>/``:

- ``overview`` — the discovery window, the bars and returns per timeframe, the excluded days;
- ``distributions`` (EDA-002), ``dependence`` (EDA-003), ``seasonality`` (EDA-004), ``trend``
  (EDA-005) and ``horizons`` (EDA-006), each with its tables and figures;
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
from xq.research.eda import dependence, distributions, horizons, seasonality, trend
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
_TOP_EPISODES = 10
#: Longest table rendered in a Markdown section; longer ones are in their CSV files.
_MAX_ROWS = 60


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
    _dependence(builder.section("dependence", "Serial dependence (EDA-003)"), run, returns)
    _seasonality(builder.section("seasonality", "Seasonality and sessions (EDA-004)"), run, returns)
    _trend(builder.section("trend", "Trend and reversion (EDA-005)"), run, inputs, returns)
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


def _dependence(section: Section, run: RunContext, returns: dict[Timeframe, pd.DataFrame]) -> None:
    eda = run.cfg.eda_config()
    sessions = run.cfg.sessions_config()
    level = eda.dependence.ci_level
    section.text(
        f"Autocorrelations up to one trading day of lags (at least {eda.dependence.min_lags}), "
        f"with pointwise {level:.0%} bands: i.i.d. (z / sqrt(n)) and heteroskedasticity-robust. "
        "Only lags outside the robust band are flagged. Ljung-Box and ARCH-LM tests are STAT-002."
    )
    summaries = []
    for tf in eda.timeframes:
        r = returns[tf]["ret"].to_numpy(dtype=np.float64)
        nlags = dependence.lags_for(
            bars_per_trading_day(tf, sessions), eda.dependence.min_lags, len(r)
        )
        if nlags < 1:
            continue
        table = dependence.dependence_table(r, nlags, level)
        summary = dependence.dependence_summary(table)
        summary.insert(0, "timeframe", tf.value)
        summaries.append(summary)
        section.table(
            f"acf-{tf.value}",
            table,
            caption=f"ACF and PACF of {tf.value} returns, |returns| and squared returns",
            max_rows=0,
        )
        flagged = table.loc[table["significant"]]
        section.table(
            f"significant-{tf.value}",
            flagged,
            caption=f"{tf.value}: lags outside the robust band",
            max_rows=_MAX_ROWS,
        )
        section.figure(
            f"acf-{tf.value}",
            dependence.dependence_figure(table, f"{tf.value} returns"),
            caption=f"ACF of {tf.value} returns, |returns| and squared returns; PACF of returns",
        )
    if summaries:
        section.table(
            "summary", pd.concat(summaries, ignore_index=True), caption="Dependence summary"
        )


def _seasonality(section: Section, run: RunContext, returns: dict[Timeframe, pd.DataFrame]) -> None:
    eda = run.cfg.eda_config()
    config = eda.seasonality
    windows = seasonality.windows_config(run.cfg.sessions_config(), config.event_windows)
    alpha = config.alpha
    section.text(
        "Effect = bucket mean minus overall mean (and in overall standard deviations). Standard "
        "errors are cluster-robust (trading week; calendar month for months); intervals are "
        f"Bonferroni-corrected within each family (family-wise level {alpha:g}). Split-half "
        "stability: same sign in both halves of the trading days and no significant difference "
        "between them. Unstable effects are not a basis for hypotheses; every effect must be "
        "validated out of sample."
    )
    intraday = returns[config.intraday_timeframe]
    daily = returns[Timeframe.D1]
    events = returns[config.event_timeframe]
    tables = []
    if len(intraday):
        weeks = seasonality.week_clusters(intraday["trading_day"])
        labels = seasonality.hour_of_week_labels(intraday["bar_start"])
        order = [h for h in seasonality.hour_of_week_order() if (labels == h).any()]
        hours = seasonality.one_hot(labels, order)
        tables.append(
            seasonality.family_effects(intraday, hours, weeks, alpha=alpha, family="hour_of_week")
        )
        inside = seasonality.membership(intraday["bar_start"], windows)
        names = [*windows.sessions, *windows.overlaps]
        tables.append(
            seasonality.family_effects(
                intraday, inside[names], weeks, alpha=alpha, family="session"
            )
        )
    if len(daily):
        days = daily["trading_day"]
        tables.append(
            seasonality.family_effects(
                daily,
                seasonality.day_of_week_members(days),
                seasonality.week_clusters(days),
                alpha=alpha,
                family="day_of_week",
            )
        )
        tables.append(
            seasonality.family_effects(
                daily,
                seasonality.month_members(days),
                seasonality.month_clusters(days),
                alpha=alpha,
                family="month",
            )
        )
    if len(events):
        inside = seasonality.membership(events["bar_start"], windows)
        names = [n for n in windows.event_windows if n in inside.columns]
        tables.append(
            seasonality.family_effects(
                events,
                inside[names],
                seasonality.week_clusters(events["trading_day"]),
                alpha=alpha,
                family="event_window",
            )
        )
    if not tables:
        section.text("No returns in the discovery window.")
        return
    effects = pd.concat(tables, ignore_index=True)
    notable = effects.loc[effects["significant"] & (effects["stability"] == seasonality.STABLE)]
    section.table(
        "significant-stable",
        notable,
        caption="Effects whose corrected interval excludes zero and that are split-half stable",
    )
    for family in effects["family"].unique():
        chunk = effects.loc[effects["family"] == family]
        section.table(
            str(family),
            chunk,
            caption=f"All {family} effects",
            max_rows=0 if family == "hour_of_week" else None,
        )
        if family in ("hour_of_week", "day_of_week", "month"):
            section.figure(
                str(family),
                seasonality.effects_figure(
                    chunk, ["ret_bps", "abs_ret_bps", "spread_bps"], f"{family} effects"
                ),
                caption=f"{family} effects with corrected intervals (hollow: not stable)",
            )


def _trend(
    section: Section,
    run: RunContext,
    inputs: EdaInputs,
    returns: dict[Timeframe, pd.DataFrame],
) -> None:
    config = run.cfg.eda_config().trend
    section.text(
        "Descriptive only. Variance ratios of overlapping q-bar sums (Lo-MacKinlay; z* is the "
        "heteroskedasticity-robust distance from 1), sign runs against independent signs, and "
        "buy-and-hold drawdowns of the daily mid close (no costs)."
    )
    vr_returns = returns[config.variance_ratio_timeframe]["ret"].to_numpy(dtype=np.float64)
    table = trend.variance_ratio_table(vr_returns, config.variance_ratio_q)
    tf = config.variance_ratio_timeframe.value
    section.table("variance-ratios", table, caption=f"Variance ratios of {tf} returns")
    section.figure(
        "variance-ratios",
        trend.variance_ratio_figure(table, f"Variance ratios of {tf} returns"),
        caption=f"Variance ratio VR(q) of {tf} returns",
    )
    run_returns = returns[config.run_timeframe]["ret"].to_numpy(dtype=np.float64)
    runs = trend.sign_runs(run_returns)
    run_tf = config.run_timeframe.value
    section.table(
        "runs",
        pd.DataFrame([{"timeframe": run_tf, **runs}]),
        caption=f"Sign runs of {run_tf} returns",
    )
    section.table(
        "run-lengths",
        trend.run_length_counts(run_returns),
        caption=f"Run lengths of {run_tf} returns against independent signs",
    )
    daily = inputs.bars[Timeframe.D1]
    if len(daily):
        prices = pd.Series(
            daily["close"].to_numpy(dtype=np.float64), index=list(daily["trading_day"])
        )
        summary = trend.drawdown_summary(prices)
        section.table(
            "drawdown", pd.DataFrame([summary]), caption="Buy-and-hold drawdowns (daily mid)"
        )
        episodes = trend.drawdown_episodes(prices)
        deepest = episodes.sort_values(["depth", "peak"], ascending=[False, True])
        section.table(
            "episodes",
            deepest.head(_TOP_EPISODES).reset_index(drop=True),
            caption=f"The {_TOP_EPISODES} deepest drawdown episodes",
        )
        section.figure(
            "underwater",
            trend.underwater_figure(prices, "Buy and hold, daily mid close"),
            caption="Daily mid close and its drawdown from the running peak",
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
