"""The baseline board runner (BASE-005), with the revised H-0001 (ADR 0035, ADR 0041, ADR 0061).

`run_baseline_board(run, dataset_id, board)` puts every baseline through the same cost model and
the same metrics, inside an experiment run:

1. **Folds.** The board's walk-forward splitter on the dataset's decision times, purged by the
   first target's ``label_end``. The out-of-sample (OOS) decisions are the union of the test
   windows, and their trading days the OOS days.
2. **Forecast baselines** (BASE-001, and BASE-003's ``ar1``) run through `run_walk_forward` for
   each target. The board reports their forecast metrics, a stationary-bootstrap interval of the
   mean loss, and a one-sided Diebold-Mariano test of beating ``zero_return`` (regression
   baselines; its horizon is the target horizon in base bars). Every forecast baseline except
   ``zero_return`` (always flat) also becomes a strategy: the sign of the forecast, or of
   ``p - 0.5`` for ``climatology``, re-decided at every OOS decision. Their models are fitted,
   so their **evaluation period** is the walk-forward test folds (``test_folds``).
3. **Rule baselines** (BASE-002) run on the signal bars of every timeframe in
   ``signal_timeframes`` (``1d`` and ``1h``), named ``<name>@<timeframe>``; lookbacks count bars
   of the signal timeframe. When a ``vol_target`` is configured, each rule also runs
   volatility-targeted (``<name>_vol@<timeframe>``), its realized volatility annualized with the
   signal bars per year (the gate periods per year for daily bars, times the bars in a regular
   trading day below that). A rule's parameters are fixed in advance, so it needs no training
   window: it is screened over **every** decision of the dataset, and its **evaluation period**
   (``full_history``) runs from the dataset's first decision to its end, before the vault: every
   rule shares one evaluation start (C-29, ADR 0064). Its warm-up (`rule_warmup`, computed from
   its parameters) may read signal bars from before the dataset's start, up to that length: the
   same source, bar build and price basis, complete bars only, before the vault, without the
   days the spec excludes, and every trading day they touch gated by the dataset's quality run
   (`pre_start_bars`). Missing or gate-failed pre-start bars stop the board (`BoardError`). A
   rule whose exposure is non-zero before its warm-up bar means the warm-up formula is wrong,
   and the board stops too.
4. **Screening.** Each strategy's exposures go through the vectorized screener (BT-002) with the
   configured cost model, on the usable quotes of the dataset's source (loaded a month at a time
   and reduced to the quotes the screener can read, `required_quotes`), with slippage from the
   target set's sigma-hat of 1-minute returns.
5. **Metrics** are computed on daily net returns of every trading day of the strategy's evaluation
   period (0 before its first fill), under the gate conventions (VAL-007): annualized with
   ``cfg.gate_periods_per_year()``; stationary bootstrap with ``n_boot`` resamples and a
   Politis-White mean block length of at least ``min_block_days``; one-sided p-values. Sharpe,
   annual return, annual volatility, Sortino and maximum drawdown come with bootstrap intervals;
   the Sharpe ratio also with its three standard errors (VAL-001), the probabilistic Sharpe ratio,
   the minimum track record length and the random-entry null p-value (share of
   ``random_entry_seeds`` random-entry versions with a Sharpe ratio at least as high). The
   random-entry template is the strategy's positions on its evaluation decisions only, so random
   episodes never land in a rule's warm-up.
6. **The fold-aligned view** of every strategy is the same screen's daily net returns restricted
   to the OOS days: identical days for every strategy and for every later candidate. For a rule
   it is a view of the same configuration, not a separate screen and not a trial (ADR 0041); the
   report shows its Sharpe ratio, annual return and net P&L as a descriptive comparison (no
   p-values).
7. **Trials.** Every strategy is one trial of the run's hypothesis family, evaluated on test data
   (its evaluation period), with its annualized Sharpe ratio and daily returns (EXP-004);
   ``zero_return`` is not a strategy and random-entry draws are a reference distribution, not
   trials. After all of them are recorded, each strategy's deflated Sharpe ratio uses the
   family's gated trial count (``effective`` or ``raw``); the report shows both counts and flags a
   raw/effective ratio above the review ratio.
8. **Slices.** The slices the run's hypothesis declared (`xq.robustness.slicing.run_slices`, read
   from its registered text) break down each strategy's evaluation-period daily net P&L and the
   trades entered in it (`slice_pnl`). They are descriptive: no p-values, no trials. A slice that
   cannot be computed (a regime slice before REG-007) is reported, not fatal.
9. **Report** under ``reports/baselines/<dataset_id>/<run_id>/``: ``board.md``, ``board.json``,
   ``returns.parquet`` (kind ``baseline_returns``: the fold-aligned daily net returns of every
   strategy on every OOS day, for paired tests against later candidates, the validation adapter,
   the registry's backtest history and the vault interval) and ``returns_evaluation.parquet``
   (kind ``baseline_returns_evaluation``: each strategy's daily net returns over its evaluation
   period, missing outside it), each recorded as a run artifact. Every net figure carries the
   cost model's label — "screening, placeholder costs" while it is provisional (ADR 0032).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from scipy.stats import t as student_t
from sqlalchemy import Engine

from xq.backtest.costs import CostModel
from xq.backtest.metrics import (
    drawdown_metrics,
    path_max_drawdowns,
    return_metrics,
    trade_metrics,
)
from xq.backtest.vectorized import BacktestResult, required_quotes, run_vectorized
from xq.core.config import AppConfig, GateConventions, gates_hash
from xq.core.errors import ConfigError, XQError
from xq.core.seeds import derive_seed
from xq.core.time import trading_day, trading_day_bounds, trading_days
from xq.core.types import Timeframe
from xq.data.calendar import NAT_NS, MarketClock, regular_trading_day
from xq.data.catalog import Catalog
from xq.data.sessions import build_session_table
from xq.datasets.base_features import AVAILABLE_AT
from xq.datasets.builder import load_dataset, read_manifest, usable_quotes
from xq.datasets.spec import DatasetSpec
from xq.models.base import ModelConfig
from xq.models.baselines import (
    SIGNAL_COLUMNS,
    RuleStrategyConfig,
    VolTargetConfig,
    forecast_baseline,
    positions_at,
    random_entry_null,
    random_walk_columns,
    rule_exposure,
    rule_warmup,
    signal_bars,
)
from xq.quality.gate import QualityGateError, gate_partitions
from xq.robustness.costs_stress import CostScenario, stressed_costs
from xq.robustness.slicing import (
    SESSION,
    VOLATILITY,
    DeclaredSlices,
    SliceError,
    run_slices,
    slice_pnl,
)
from xq.targets.base import market_horizon, target_values
from xq.targets.kinds import target_kind
from xq.tracking import registry
from xq.tracking.trials import trial_count
from xq.validation.dsr import deflated_sharpe_for_family, probabilistic_sharpe
from xq.validation.forecast_eval import diebold_mariano, loss_series
from xq.validation.sharpe import (
    bootstrap_distribution,
    bootstrap_sharpe,
    estimate,
    gate_block_length,
    min_track_record_length,
    sharpe_ratio,
)
from xq.validation.splitters import Fold, WalkForwardConfig, WalkForwardSplitter
from xq.validation.walkforward import run_walk_forward

if TYPE_CHECKING:
    from xq.tracking.runs import RunContext

FloatArray = npt.NDArray[np.float64]
REPORT_DIR = "baselines"
REFERENCE_FORECAST = "zero_return"
#: Evaluation periods: a rule's full pre-vault history after its warm-up, or the test folds.
FULL_HISTORY, TEST_FOLDS = "full_history", "test_folds"
#: Separates a rule's name from its signal timeframe in a strategy name (``tsmom_252@1d``).
TIMEFRAME_SEPARATOR = "@"
SLICES_LABEL = "descriptive (not tested: no p-values, no trials)"
#: Artifact kinds of the fold-aligned and the evaluation-period daily returns.
RETURNS_ARTIFACT = "baseline_returns"
EVALUATION_RETURNS_ARTIFACT = "baseline_returns_evaluation"
_BPS = 1e4
#: Open trading days loaded beyond a warm-up's estimate, for early closes and missing bars.
PRE_START_MARGIN_DAYS = 5


class BoardError(XQError):
    """The board cannot be run on this dataset (no folds, a missing target or signal bars)."""


@dataclass(frozen=True)
class BoardRule:
    """One rule strategy of a board: the rule's configuration and its signal timeframe."""

    rule: RuleStrategyConfig
    timeframe: str


class BoardConfig(BaseModel):
    """The baseline board (``experiments/configs/baselines/board.yaml``), fixed in advance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    targets: list[str] = Field(min_length=1)
    walk_forward: WalkForwardConfig
    forecast_baselines: list[str] = []
    #: The signal timeframes every rule runs on (ADR 0035: ``1d`` and ``1h``).
    signal_timeframes: list[str] = []
    rules: dict[str, RuleStrategyConfig] = {}
    vol_target: VolTargetConfig | None = None
    random_entry_seeds: int = Field(default=1000, ge=0)
    ci_level: float = Field(default=0.95, gt=0, lt=1)

    @model_validator(mode="after")
    def _check(self) -> BoardConfig:
        for name in self.forecast_baselines:
            forecast_baseline(name)  # raises ConfigError for an unknown name
        if self.rules and not self.signal_timeframes:
            raise ConfigError("rule baselines need at least one signal timeframe")
        if len(set(self.signal_timeframes)) != len(self.signal_timeframes):
            raise ConfigError(f"signal timeframes repeat: {self.signal_timeframes}")
        for timeframe in self.signal_timeframes:
            try:
                Timeframe(timeframe)
            except ValueError:
                raise ConfigError(f"unknown signal timeframe {timeframe!r}") from None
        named = [n for n in self.rules if TIMEFRAME_SEPARATOR in n]
        if named:
            raise ConfigError(f"rule names must not contain {TIMEFRAME_SEPARATOR!r}: {named}")
        return self

    def strategies(self) -> dict[str, BoardRule]:
        """Every rule strategy on every signal timeframe, named ``<name>@<timeframe>``: each rule
        as configured, plus ``<name>_vol`` with a volatility target."""
        expanded = dict(self.rules)
        if self.vol_target is not None:
            for name, rule in self.rules.items():
                if not rule.vol_target:
                    expanded[f"{name}_vol"] = rule.model_copy(update={"vol_target": True})
        return {
            f"{name}{TIMEFRAME_SEPARATOR}{timeframe}": BoardRule(rule, timeframe)
            for timeframe in self.signal_timeframes
            for name, rule in expanded.items()
        }

    def config_hash(self) -> str:
        """16-hex SHA-256 of the board's canonical JSON."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_board_config(path: Path) -> BoardConfig:
    """Read and validate a board configuration file.

    Raises:
        ConfigError: if the file is missing, malformed or invalid.
    """
    if not path.is_file():
        raise ConfigError(f"board configuration not found: {path}")
    try:
        return BoardConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid board configuration {path}:\n{exc}") from exc


@dataclass(frozen=True)
class BoardResult:
    """The board of one run: one row per strategy and per forecast baseline, and the report."""

    strategies: pd.DataFrame
    forecasts: pd.DataFrame
    #: Fold-aligned daily net returns: every strategy on every OOS day (``returns.parquet``).
    returns: pd.DataFrame
    #: Daily net returns over each strategy's evaluation period, missing outside it
    #: (``returns_evaluation.parquet``).
    evaluation_returns: pd.DataFrame
    report_dir: Path
    cost_basis: str
    summary: dict[str, Any]


@dataclass(frozen=True)
class _Strategy:
    name: str
    kind: str
    target: str | None
    params: dict[str, Any]
    #: Target exposure per decision: every decision for a rule, the OOS decisions otherwise.
    positions: pd.Series
    #: The first decision of the evaluation period and the period's trading days.
    start: pd.Timestamp
    days: list[date]
    period: str
    timeframe: str | None = None
    warmup_bars: int | None = None
    #: Signal bars read from before the dataset's start for the warm-up (C-29).
    pre_start_bars: int | None = None


@dataclass(frozen=True)
class ScreeningContext:
    """What a board screens every strategy against: every decision of the dataset and its
    trading days, the out-of-sample decisions and days of its walk-forward folds, the quotes and
    the sigma-hat of 1-minute returns for every decision, the cost model and the market clock.
    `run_baseline_board` and the validation adapter of a board run (`xq.validation.subjects`)
    build it the same way, so a strategy screened by either gives the same daily returns."""

    dataset_id: str
    spec: DatasetSpec
    features: pd.DataFrame
    folds: list[Fold]
    decisions: pd.DatetimeIndex
    days: list[date]
    oos: pd.DatetimeIndex
    oos_days: list[date]
    costs: CostModel
    clock: MarketClock
    quotes: pd.DataFrame
    sigma: pd.Series
    capital: float
    periods_per_year: int

    def screen(
        self,
        positions: pd.Series,
        *,
        quotes: pd.DataFrame | None = None,
        costs: CostModel | None = None,
    ) -> BacktestResult:
        """`positions` (on any of the decisions) through the screener (optionally with
        stressed quotes or costs)."""
        return run_vectorized(
            positions,
            self.quotes if quotes is None else quotes,
            self.costs if costs is None else costs,
            self.clock,
            capital=self.capital,
            sigma_1m_bps=self.sigma,
        )

    def daily_returns(self, result: BacktestResult) -> pd.Series:
        """Daily net returns of a screen on every OOS trading day (0 without activity): the
        fold-aligned view."""
        return daily_returns_on(result, self.oos_days)

    def daily_sigma(self, decisions: pd.DatetimeIndex, days: list[date]) -> pd.Series:
        """Daily sigma-hat known at each day's first decision among `decisions` (the sigma-hat of
        1-minute returns scaled to a regular trading day), on `days`."""
        minutes = regular_trading_day(self.costs.sessions) / pd.Timedelta(minutes=1)
        frame = pd.DataFrame(
            {
                "day": [d.item() for d in trading_days(decisions)],
                "sigma": self.sigma.reindex(decisions).to_numpy(np.float64)
                / _BPS
                * math.sqrt(minutes),
            }
        )
        first = frame.groupby("day", sort=True)["sigma"].first()
        return first.reindex(days).rename("sigma_daily")

    def day_folds(self) -> pd.Series:
        """The walk-forward test fold of each OOS trading day (its first OOS decision's)."""
        fold_of = np.empty(len(self.features), dtype=object)
        for fold in self.folds:
            fold_of[fold.test_idx] = fold.fold_id
        position = self.features.index.get_indexer(self.oos)
        frame = pd.DataFrame(
            {"day": [d.item() for d in trading_days(self.oos)], "fold": fold_of[position]}
        )
        first = frame.groupby("day", sort=True)["fold"].first()
        return first.reindex(self.oos_days).rename("fold")


def screening_context(
    cfg: AppConfig,
    engine: Engine,
    dataset_id: str,
    board: BoardConfig,
    targets: Sequence[str],
    *,
    extra_latencies_ms: Sequence[int] = (),
) -> ScreeningContext:
    """The board's screening context on dataset `dataset_id` (see `ScreeningContext`).

    Args:
        extra_latencies_ms: Also keep the quotes a screen with this much latency added would read
            (ROB-002's latency stress); the board itself needs none.

    Raises:
        BoardError: if the dataset lacks a target or yields no fold.
    """
    manifest = read_manifest(cfg, dataset_id)
    spec = DatasetSpec.model_validate(manifest["spec"])
    features = load_dataset(cfg, dataset_id, "features")
    target_frame = load_dataset(cfg, dataset_id, "targets")
    known = set(target_frame["target"].unique())
    missing = [t for t in targets if t not in known]
    if missing:
        raise BoardError(f"dataset {dataset_id} has no targets {missing}")

    decisions = pd.DatetimeIndex(features.index)
    first = target_values(target_frame, targets[0]).reindex(decisions)
    folds = WalkForwardSplitter(board.walk_forward).split(decisions, first["label_end"])
    if not folds:
        raise BoardError(f"the walk-forward splitter gives no fold on dataset {dataset_id}")
    oos = decisions[np.unique(np.concatenate([f.test_idx for f in folds]))]
    oos_days = sorted({d.item() for d in trading_days(oos)})

    costs = CostModel.from_config(cfg, spec.instrument, engine=engine, source_id=spec.source)
    clock = MarketClock.for_range(
        cfg.sessions_config(),
        trading_day(decisions[0]) - timedelta(days=1),
        trading_day(decisions[-1]) + timedelta(days=10),
    )
    excluded = {date.fromisoformat(e["trading_day"]) for e in manifest["excluded_partitions"]}
    quotes = _screening_quotes(cfg, spec, decisions, excluded, costs, clock, extra_latencies_ms)
    sigma = sigma_1m_bps(cfg, spec, features).reindex(decisions)
    return ScreeningContext(
        dataset_id=dataset_id,
        spec=spec,
        features=features,
        folds=folds,
        decisions=decisions,
        days=sorted({d.item() for d in trading_days(decisions)}),
        oos=oos,
        oos_days=oos_days,
        costs=costs,
        clock=clock,
        quotes=quotes,
        sigma=sigma,
        capital=cfg.backtest_config().capital_usd,
        periods_per_year=cfg.gate_periods_per_year(),
    )


def run_baseline_board(
    run: RunContext,
    dataset_id: str,
    board: BoardConfig,
    *,
    targets: Sequence[str] | None = None,
    n_jobs: int = 1,
    use_cache: bool = True,
) -> BoardResult:
    """Evaluate every baseline of `board` on dataset `dataset_id` (see the module docstring).

    Args:
        targets: Targets to run the forecast baselines on (default: the board's).

    Raises:
        BoardError: if the dataset yields no fold, lacks a target or the signal bars, is too short
            for a rule's warm-up, or a rule holds a position before its warm-up ends.
    """
    cfg = run.cfg
    conventions = cfg.gates_config().conventions
    chosen = list(targets or board.targets)
    context = screening_context(cfg, run.engine, dataset_id, board, chosen)
    spec, features, folds = context.spec, context.features, context.folds
    oos, oos_days, periods = context.oos, context.oos_days, context.periods_per_year

    strategies: list[_Strategy] = []
    forecast_rows: list[dict[str, Any]] = []
    for target in chosen:
        rows, made = _forecast_baselines(
            run,
            dataset_id,
            spec,
            features,
            target,
            board,
            (oos, oos_days),
            conventions,
            n_jobs,
            use_cache,
        )
        forecast_rows.extend(rows)
        strategies.extend(made)
    strategies.extend(_rule_strategies(cfg, run.engine, context, board))

    family = _family(run)
    capital = context.capital
    evaluated: list[tuple[_Strategy, BacktestResult, pd.Series, pd.Series]] = []
    for strategy in strategies:
        result = context.screen(strategy.positions)
        returns = daily_returns_on(result, strategy.days)
        annual_sharpe = return_metrics(returns, periods)["sharpe"]
        run.record_trial(
            family_id=family,
            config={
                "board": board.config_hash(),
                "dataset_id": dataset_id,
                "strategy": strategy.name,
                "kind": strategy.kind,
                "target": strategy.target,
                "signal_timeframe": strategy.timeframe,
                "params": strategy.params,
            },
            evaluated_on_test=True,
            sharpe=annual_sharpe if math.isfinite(annual_sharpe) else None,
            returns=_trial_returns(returns),
        )
        evaluated.append((strategy, result, returns, context.daily_returns(result)))

    stats = trial_count(cfg, run.engine, family)
    use_effective = conventions.trial_count == "effective"
    declared, slice_error = _declared_slices(run)
    rows = []
    slices: dict[str, Any] = {}
    for strategy, result, returns, fold in evaluated:
        seed = derive_seed(run.run.seed, "baseline_board", strategy.name)
        fold_metrics = return_metrics(fold, periods)
        row: dict[str, Any] = {
            "strategy": strategy.name,
            "kind": strategy.kind,
            "target": strategy.target,
            "signal_timeframe": strategy.timeframe,
            "warmup_bars": strategy.warmup_bars,
            "pre_start_bars": strategy.pre_start_bars,
            "period": strategy.period,
            "evaluation_start": str(strategy.start),
            "evaluation_days": len(strategy.days),
            "cost_basis": result.cost_basis,
            **_strategy_metrics(
                returns, result, strategy.days, periods, conventions, board.ci_level, seed
            ),
            "fold_sharpe": fold_metrics["sharpe"],
            "fold_annual_return": fold_metrics["annual_return"],
            "fold_net_pnl": float(fold.sum() * capital),
        }
        row["dsr"] = _dsr(cfg, run, family, returns, periods, use_effective)
        template = strategy.positions.loc[strategy.positions.index >= strategy.start]
        row["random_entry_p"] = _random_entry_p(
            template,
            sharpe_ratio(returns.to_numpy(np.float64)),
            context,
            strategy.days,
            board,
            seed,
        )
        rows.append(row)
        if declared is not None:
            slices[strategy.name] = _strategy_slices(declared, context, strategy, result, returns)

    strategy_frame = pd.DataFrame(rows)
    if len(strategy_frame):
        for column in ("warmup_bars", "pre_start_bars"):
            strategy_frame[column] = strategy_frame[column].astype("Int64")
    forecast_frame = pd.DataFrame(forecast_rows)
    returns_frame = pd.DataFrame(
        {s.name: fold.to_numpy() for s, _, _, fold in evaluated},
        index=pd.Index([d.isoformat() for d in oos_days], name="trading_day"),
    )
    evaluation_frame = pd.DataFrame(
        {s.name: r.reindex(context.days).to_numpy() for s, _, r, _ in evaluated},
        index=pd.Index([d.isoformat() for d in context.days], name="trading_day"),
    )
    summary = {
        "dataset_id": dataset_id,
        "run_id": run.run_id,
        "confirmatory": run.confirmatory,
        "hypothesis_family": family,
        "cost_model": cfg.backtest_config().cost_model,
        "cost_basis": context.costs.result_label,
        "gates_hash": gates_hash(cfg.gates_config()),
        "board_hash": board.config_hash(),
        "base_timeframe": spec.base_timeframe.value,
        "signal_timeframes": list(board.signal_timeframes),
        "targets": chosen,
        "history_start": str(context.decisions[0]),
        "history_end": str(context.decisions[-1]),
        "history_days": len(context.days),
        "oos_start": str(oos[0]),
        "oos_end": str(oos[-1]),
        "oos_days": len(oos_days),
        "folds": len(folds),
        "periods_per_year": periods,
        "bootstrap": conventions.bootstrap.model_dump(mode="json"),
        "trials": {
            "raw": stats.n_trials,
            "effective": stats.effective_n,
            "gated": conventions.trial_count,
            "review": conventions.needs_trial_review(stats.n_trials, stats.effective_n),
        },
        "slices": {
            "label": SLICES_LABEL,
            "declared": list(declared.names) if declared is not None else [],
            "hypothesis": (
                f"{declared.hypothesis_id} v{declared.version}" if declared is not None else None
            ),
            "error": slice_error,
            "strategies": slices,
        },
    }
    report_dir = _write_report(
        run, summary, strategy_frame, forecast_frame, returns_frame, evaluation_frame
    )
    for row in rows:
        for key in (
            "sharpe",
            "sharpe_ci_low",
            "sharpe_ci_high",
            "sharpe_p",
            "dsr",
            "trade_count",
            "fold_sharpe",
        ):
            value = row.get(key)
            if value is not None and math.isfinite(value):
                run.log_metric(f"board/{row['strategy']}/{key}", float(value))
    return BoardResult(
        strategy_frame,
        forecast_frame,
        returns_frame,
        evaluation_frame,
        report_dir,
        context.costs.result_label,
        summary,
    )


def _forecast_baselines(
    run: RunContext,
    dataset_id: str,
    spec: DatasetSpec,
    features: pd.DataFrame,
    target: str,
    board: BoardConfig,
    test_folds: tuple[pd.DatetimeIndex, list[date]],
    conventions: GateConventions,
    n_jobs: int,
    use_cache: bool,
) -> tuple[list[dict[str, Any]], list[_Strategy]]:
    """Forecast rows and forecast-sign strategies of every forecast baseline on one target (on
    the OOS decisions `test_folds`, with their trading days)."""
    oos, oos_days = test_folds
    label = _horizon_label(run.cfg, spec, target)
    horizon_bars = max(
        1,
        round(
            market_horizon(label, regular_trading_day(run.cfg.sessions_config()))
            / spec.base_timeframe.duration
        ),
    )
    losses: dict[str, pd.Series] = {}
    rows: list[dict[str, Any]] = []
    strategies: list[_Strategy] = []
    for name in board.forecast_baselines:
        model = forecast_baseline(name)
        params: dict[str, Any] = {}
        columns: list[str] = []
        if name == "random_walk":
            opened, closed = random_walk_columns(label, spec.base_timeframe.value)
            if target.endswith("_vol") or not {opened, closed} <= set(features.columns):
                continue  # not applicable: no bar of the horizon, or a scaled target
            params, columns = {"open": opened, "close": closed}, [opened, closed]
        elif name == "ar1":
            if target.endswith("_vol"):
                continue  # not applicable: the AR forecasts returns, not scaled targets
            params = {"open": "open", "close": "close", "horizon_bars": horizon_bars}
            columns = ["open", "close"]
        result = run_walk_forward(
            run,
            dataset_id,
            target,
            model,
            ModelConfig(name=name, params=params, features=columns),
            board.walk_forward,
            n_jobs=n_jobs,
            use_cache=use_cache,
            record_trial=False,
        )
        predictions = result.predictions
        known = predictions.loc[predictions["y_true"].notna()]
        classification = model.task == "classification"
        forecast = known["p_raw"] if classification else known["y_pred"]
        loss = loss_series(
            known["y_true"],
            forecast.clip(0.0, 1.0) if classification else forecast,
            "log" if classification else "squared",
        )
        losses[name] = loss
        seed = derive_seed(run.run.seed, "baseline_board_loss", f"{name}:{target}")
        low, high = _mean_ci(loss.to_numpy(np.float64), conventions, board.ci_level, seed)
        row: dict[str, Any] = {
            "target": target,
            "horizon": label,
            "baseline": name,
            "task": model.task,
            "loss": "log" if classification else "squared",
            "mean_loss": float(loss.mean()) if len(loss) else math.nan,
            "mean_loss_ci_low": low,
            "mean_loss_ci_high": high,
            **{k: float(v) for k, v in result.metrics.items()},
        }
        rows.append(row)
        if name != REFERENCE_FORECAST:
            raw = predictions["p_raw"] - 0.5 if classification else predictions["y_pred"]
            signs = np.sign(raw.reindex(oos).to_numpy(np.float64))
            positions = pd.Series(np.nan_to_num(signs, nan=0.0), index=oos, name="exposure")
            strategies.append(
                _Strategy(
                    f"{name}:{target}",
                    "forecast_sign",
                    target,
                    params,
                    positions,
                    start=oos[0],
                    days=oos_days,
                    period=TEST_FOLDS,
                )
            )
    reference = losses.get(REFERENCE_FORECAST)
    for row in rows:
        row["dm_vs_zero_p"] = math.nan
        row["dm_vs_zero_statistic"] = math.nan
        if (
            reference is None
            or row["task"] != "regression"
            or row["baseline"] == REFERENCE_FORECAST
        ):
            continue
        mine = losses[row["baseline"]]
        common = mine.index.intersection(reference.index)
        test = diebold_mariano(mine.loc[common], reference.loc[common], horizon=horizon_bars)
        row["dm_vs_zero_statistic"] = test.statistic
        # one-sided: this baseline has the lower expected loss (a negative statistic)
        row["dm_vs_zero_p"] = (
            float(student_t.cdf(test.statistic, df=test.n - 1))
            if math.isfinite(test.statistic)
            else math.nan
        )
    return rows, strategies


def _rule_strategies(
    cfg: AppConfig, engine: Engine, context: ScreeningContext, board: BoardConfig
) -> list[_Strategy]:
    """Every rule strategy on every signal timeframe, positioned on every decision, warmed up on
    its own pre-start bars and evaluated from the dataset's first decision (module docstring,
    item 3)."""
    strategies = []
    start = pd.Timestamp(context.decisions[0])
    days = list(context.days)
    for name, entry in board.strategies().items():
        rule, timeframe = entry.rule, entry.timeframe
        warmup = rule_warmup(rule, board.vol_target)
        bars, bar_periods, pre_start = warmed_signal_bars(
            cfg, engine, context, name, timeframe, warmup
        )
        exposure = rule_exposure(
            bars, rule, vol_target=board.vol_target, periods_per_year=bar_periods
        )
        early = exposure.iloc[: warmup - 1]
        early = early.loc[early.to_numpy() != 0]
        if len(early):
            raise BoardError(
                f"rule {name} holds a position at {early.index[0]}, before its warm-up bar "
                f"(warm-up {warmup} {timeframe} bars): the warm-up formula is wrong"
            )
        strategies.append(
            _Strategy(
                name,
                "rule_vol" if rule.vol_target else "rule",
                None,
                {"rule": rule.rule, **rule.params},
                positions_at(context.decisions, exposure),
                start=start,
                days=days,
                period=FULL_HISTORY,
                timeframe=timeframe,
                warmup_bars=warmup,
                pre_start_bars=pre_start,
            )
        )
    return strategies


def warmed_signal_bars(
    cfg: AppConfig,
    engine: Engine,
    context: ScreeningContext,
    name: str,
    timeframe: str,
    warmup: int,
) -> tuple[pd.DataFrame, int, int]:
    """Rule `name`'s signal bars of `timeframe`: the dataset's, preceded by as many pre-start
    bars as its warm-up needs to be complete at the dataset's first decision (C-29).

    Returns:
        The signal bars, the signal bars per year (`rule_signal_bars`) and how many of the bars
        come from before the dataset's start.

    Raises:
        BoardError: if the dataset has no context bars of `timeframe`, or the pre-start bars the
            warm-up needs are missing or fail the quality gate.
    """
    bars, periods = rule_signal_bars(cfg, context.features, timeframe, context.periods_per_year)
    first = pd.Timestamp(context.decisions[0])
    needed = warmup - int((bars.index <= first).sum())
    if needed <= 0:
        return bars, periods, 0
    before = pre_start_bars(
        cfg, engine, context.spec, Timeframe(timeframe), pd.Timestamp(bars.index[0]), needed, name
    )
    return pd.concat([before, bars]), periods, needed


def pre_start_bars(
    cfg: AppConfig,
    engine: Engine,
    spec: DatasetSpec,
    timeframe: Timeframe,
    before: pd.Timestamp,
    count: int,
    name: str,
) -> pd.DataFrame:
    """The last `count` signal bars of `timeframe` available before `before`, read from the
    dataset's own source, bar build and price basis: complete bars only, without the trading days
    the spec excludes, never from the vault (the catalog enforces it), and every trading day they
    touch passed by the dataset's quality run (DQ-007). Indexed by availability, like
    `signal_bars`.

    Raises:
        BoardError: naming rule `name`, if fewer than `count` such bars exist or one of their days
            fails the quality gate (a FAIL result, or no result in the quality run).
    """
    sessions = cfg.sessions_config()
    per_day = max(1, math.floor(regular_trading_day(sessions) / timeframe.duration))
    open_days = math.ceil(count / per_day) + PRE_START_MARGIN_DAYS
    last = trading_day(before)
    table = build_session_table(sessions, last - timedelta(days=2 * open_days + 31), last)
    opened = sorted(
        pd.Timestamp(d).date()
        for d in table.loc[table["is_open"].astype(bool), "trading_day"]
        if pd.Timestamp(d).date() <= last
    )
    load_start = trading_day_bounds(opened[-min(open_days, len(opened))])[0]
    loaded = Catalog(cfg).load_bars(
        spec.source,
        spec.instrument,
        timeframe,
        spec.price_basis,
        load_start,
        before,
        build=spec.bar_build,
    )
    excluded = {e.trading_day for e in spec.exclusions}
    keep = loaded["is_complete"].to_numpy(bool) & ~loaded["trading_day"].isin(excluded).to_numpy()
    keep &= (loaded[AVAILABLE_AT] < before).to_numpy()
    chosen = loaded.loc[keep].tail(count)
    if len(chosen) < count:
        raise BoardError(
            f"rule {name} needs {count} {timeframe.value} signal bars before the dataset's start "
            f"for its warm-up, but only {len(chosen)} complete bars are available before {before}"
        )
    if spec.quality_run_id is None:
        raise BoardError("the dataset's spec names no quality run")
    try:
        gate_partitions(engine, spec.quality_run_id, set(chosen["trading_day"]), {})
    except QualityGateError as exc:
        raise BoardError(
            f"the pre-start {timeframe.value} signal bars of rule {name} fail the quality gate: "
            f"{exc}"
        ) from exc
    return pd.DataFrame(
        {c: chosen[c].to_numpy(np.float64) for c in SIGNAL_COLUMNS},
        index=pd.DatetimeIndex(chosen[AVAILABLE_AT], name="available_at"),
    )


def rule_signal_bars(
    cfg: AppConfig, features: pd.DataFrame, timeframe: str, periods: int
) -> tuple[pd.DataFrame, int]:
    """The signal bars of `timeframe` in `features`, and the signal bars per year that
    volatility targeting annualizes with (one bar a day for 1d, more below it).

    Raises:
        BoardError: if the features have no context bars of the signal timeframe.
    """
    if Timeframe(timeframe) is not Timeframe.D1:
        bars_per_day = regular_trading_day(cfg.sessions_config()) / Timeframe(timeframe).duration
        periods = round(periods * bars_per_day)
    try:
        bars = signal_bars(features, f"ctx_{timeframe}_")
    except KeyError as exc:
        raise BoardError(
            f"the dataset has no {timeframe} context bars for the rule baselines"
        ) from exc
    return bars, periods


def rule_positions(
    bars: pd.DataFrame,
    rule: RuleStrategyConfig,
    vol_target: VolTargetConfig | None,
    bar_periods: int,
    decisions: pd.DatetimeIndex,
) -> pd.Series:
    """A rule baseline's target exposure at each decision (its latest available signal bar)."""
    exposure = rule_exposure(bars, rule, vol_target=vol_target, periods_per_year=bar_periods)
    return positions_at(decisions, exposure)


def _screening_quotes(
    cfg: AppConfig,
    spec: DatasetSpec,
    decisions: pd.DatetimeIndex,
    excluded: set[date],
    costs: CostModel,
    clock: MarketClock,
    extra_latencies_ms: Sequence[int] = (),
) -> pd.DataFrame:
    """The usable quotes a screen of `decisions` reads, loaded a month of trading days at a time
    (with each extra latency too, when given)."""
    variants = [costs] + [
        stressed_costs(costs, CostScenario(f"latency_+{ms}ms", extra_latency_ms=ms))
        for ms in extra_latencies_ms
    ]
    days = trading_days(decisions)
    months = np.array([d.item().strftime("%Y-%m") for d in days])
    vault = pd.Timestamp(cfg.vault.start)
    parts = []
    for month in np.unique(months):
        chunk = decisions[months == month]
        start = trading_day_bounds(trading_day(chunk[0]))[0]
        end = trading_day_bounds(trading_day(chunk[-1]))[1]
        for variant in variants:
            intended = clock.advance(_ns(chunk), variant.latency.value)
            intended = intended[intended != NAT_NS]
            if len(intended):
                reach = pd.Timestamp(int(intended.max()), tz="UTC") + variant.max_fill_delay
                end = max(end, reach + pd.Timedelta(1, "ns"))
        ticks = usable_quotes(cfg, spec, start, min(end, vault), excluded)
        rows = np.unique(
            np.concatenate([required_quotes(ticks, chunk, v, clock) for v in variants])
        )
        parts.append(ticks.iloc[rows])
    quotes = pd.concat(parts).drop_duplicates(subset=["raw_file_id", "row_num"])
    quotes = quotes.sort_values(["ts_utc", "raw_file_id", "row_num"], kind="stable")
    return quotes.loc[:, ["ts_utc", "bid", "ask"]].reset_index(drop=True)


def sigma_1m_bps(cfg: AppConfig, spec: DatasetSpec, features: pd.DataFrame) -> pd.Series:
    """Sigma-hat of 1-minute log returns (bps) at each decision, from the target set's estimator."""
    if spec.target_set is None:
        raise BoardError("the board needs a dataset with a target set")
    definition = cfg.target_set(spec.target_set.name, spec.target_set.version)
    kind = target_kind(definition.kind)
    close = pd.Series(features["close"].to_numpy(np.float64), index=features.index)
    rate = kind.sigma(close, definition, spec.base_timeframe.duration)  # per square-root minute
    return (rate * _BPS).rename("sigma_1m_bps")


def _horizon_label(cfg: AppConfig, spec: DatasetSpec, target: str) -> str:
    """The horizon label of `target` in its target set (``1h`` for ``fwd_ret_mid_1h``)."""
    if spec.target_set is None:
        raise BoardError("the board needs a dataset with a target set")
    definition = cfg.target_set(spec.target_set.name, spec.target_set.version)
    stem = target.removesuffix("_vol")
    labels = [h for h in definition.horizons if stem.endswith(f"_{h}")]
    if not labels:
        raise BoardError(f"cannot tell the horizon of target {target!r}")
    return max(labels, key=len)


def daily_returns_on(result: BacktestResult, days: list[date]) -> pd.Series:
    """Daily net returns on every OOS trading day; 0 before the first fill."""
    daily = result.daily["return"] if len(result.daily) else pd.Series(dtype="float64")
    values = daily.reindex(days).fillna(0.0).to_numpy(np.float64)
    return pd.Series(values, index=pd.Index(days, name="trading_day"), name="return")


def _trial_returns(returns: pd.Series) -> pd.Series:
    """Daily returns indexed by each trading day's start instant (tz-aware, as trials need)."""
    starts = pd.DatetimeIndex([trading_day_bounds(d)[0] for d in returns.index], name="time")
    return pd.Series(returns.to_numpy(np.float64), index=starts)


def _strategy_metrics(
    returns: pd.Series,
    result: BacktestResult,
    days: list[date],
    periods: int,
    conventions: GateConventions,
    level: float,
    seed: int,
) -> dict[str, float]:
    """Metrics of one strategy's daily returns under the gate conventions (module docstring)."""
    r = returns.to_numpy(np.float64)
    root = math.sqrt(periods)
    metrics = return_metrics(returns, periods)
    metrics.update(drawdown_metrics(pd.Series(result.capital * (1 + np.cumsum(r))), result.capital))
    metrics.update(trade_metrics(result.trades))
    block = gate_block_length(
        r, conventions.bootstrap.block_length, conventions.bootstrap.min_block_days
    )
    n_boot = conventions.bootstrap.n_boot
    boot = bootstrap_sharpe(r, n_boot=n_boot, mean_block=block, seed=seed, level=level)
    metrics.update(
        sharpe_ci_low=boot.ci_low * root,
        sharpe_ci_high=boot.ci_high * root,
        sharpe_p=boot.p_value,
        block_length=block,
    )
    statistics: dict[str, Callable[[FloatArray], FloatArray]] = {
        "annual_return": lambda d: d.mean(axis=1) * periods,
        "annual_volatility": lambda d: d.std(axis=1, ddof=1) * root,
        "sortino": lambda d: _row_sortino(d) * root,
        "max_drawdown": _row_max_drawdown,
    }
    alpha = (1 - level) / 2
    for name, statistic in statistics.items():
        low, high = math.nan, math.nan
        if len(r) >= 3 and np.std(r) > 0:
            draws = bootstrap_distribution(r, statistic, n_boot=n_boot, mean_block=block, seed=seed)
            valid = draws[np.isfinite(draws)]
            if len(valid):
                low, high = (float(q) for q in np.quantile(valid, [alpha, 1 - alpha]))
        metrics[f"{name}_ci_low"], metrics[f"{name}_ci_high"] = low, high
    est = estimate(r)
    metrics.update(
        se_iid=est.se_iid * root,
        se_non_normal=est.se_non_normal * root,
        se_hac=est.se_hac * root,
        psr=probabilistic_sharpe(est.sharpe, est.n, est.skew, est.kurtosis)
        if math.isfinite(est.sharpe)
        else math.nan,
    )
    confidence = 0.95
    trl = (
        min_track_record_length(est.sharpe, est.skew, est.kurtosis, alpha=1 - confidence)
        if math.isfinite(est.sharpe) and math.isfinite(est.skew)
        else math.inf
    )
    metrics["min_track_record_days"] = trl
    metrics["oos_days_over_min_trl"] = len(r) / trl if trl > 0 else math.nan
    daily = result.daily.loc[result.daily.index.isin(days)]
    for column in ("gross_pnl", "spread_cost", "slippage_cost", "commission", "financing"):
        metrics[column] = float(daily[column].sum()) if len(daily) else 0.0
    metrics["net_pnl"] = float(r.sum() * result.capital)
    metrics["missed_decisions"] = float(len(result.missed))
    metrics["closed_market_decisions"] = float(len(result.closed))
    return metrics


def _row_sortino(draws: FloatArray) -> FloatArray:
    downside = np.sqrt(np.mean(np.minimum(draws, 0.0) ** 2, axis=1))
    safe = np.where(downside > 0, downside, 1.0)
    result: FloatArray = np.where(downside > 0, draws.mean(axis=1) / safe, np.nan)
    return result


def _row_max_drawdown(draws: FloatArray) -> FloatArray:
    return path_max_drawdowns(1 + np.cumsum(draws, axis=1))


def _mean_ci(
    values: FloatArray, conventions: GateConventions, level: float, seed: int
) -> tuple[float, float]:
    if len(values) < 3 or np.std(values) == 0:
        return math.nan, math.nan
    block = gate_block_length(
        values, conventions.bootstrap.block_length, conventions.bootstrap.min_block_days
    )
    draws = bootstrap_distribution(
        values,
        lambda d: d.mean(axis=1),
        n_boot=conventions.bootstrap.n_boot,
        mean_block=block,
        seed=seed,
    )
    alpha = (1 - level) / 2
    low, high = np.quantile(draws, [alpha, 1 - alpha])
    return float(low), float(high)


def _dsr(
    cfg: AppConfig,
    run: RunContext,
    family: str,
    returns: pd.Series,
    periods: int,
    use_effective: bool,
) -> float:
    r = returns.to_numpy(np.float64)
    if not math.isfinite(sharpe_ratio(r)):
        return math.nan
    result = deflated_sharpe_for_family(
        cfg, run.engine, family, r, periods_per_year=periods, use_effective=use_effective
    )
    return result.dsr


def _random_entry_p(
    positions: pd.Series,
    observed: float,
    context: ScreeningContext,
    days: list[date],
    board: BoardConfig,
    seed: int,
) -> float:
    """Share of random-entry versions of `positions` (the evaluation decisions' positions) with a
    Sharpe ratio on `days` at least the strategy's (plus one)."""
    if board.random_entry_seeds == 0 or not (positions != 0).any() or not math.isfinite(observed):
        return math.nan
    null = []
    for draw in random_entry_null(positions, board.random_entry_seeds, seed=seed):
        screened = context.screen(draw)
        null.append(sharpe_ratio(daily_returns_on(screened, days).to_numpy(np.float64)))
    values = np.array(null, dtype=np.float64)
    values = values[np.isfinite(values)]
    return (1 + int(np.sum(values >= observed))) / (1 + len(values))


def _declared_slices(run: RunContext) -> tuple[DeclaredSlices | None, str | None]:
    """The slices the run's hypothesis declared, or why they cannot be read."""
    try:
        return run_slices(run.engine, run.run_id), None
    except SliceError as exc:
        return None, str(exc)


def _strategy_slices(
    declared: DeclaredSlices,
    context: ScreeningContext,
    strategy: _Strategy,
    result: BacktestResult,
    returns: pd.Series,
) -> dict[str, Any]:
    """Descriptive slices of one strategy's evaluation-period daily net P&L and the trades entered
    in it (module docstring, item 8); a slice that cannot be computed is reported."""
    trades = result.trades
    if len(trades):
        trades = trades.loc[pd.DatetimeIndex(trades["entry_time"]) >= strategy.start]
    sigma = None
    if VOLATILITY in declared.names:
        decisions = context.decisions[context.decisions >= strategy.start]
        sigma = context.daily_sigma(decisions, strategy.days)
    try:
        report = slice_pnl(
            declared,
            returns * context.capital,
            trades,
            capital=context.capital,
            periods_per_year=context.periods_per_year,
            sessions=context.costs.sessions,
            sigma=sigma,
        )
    except SliceError as exc:
        return {"error": str(exc)}
    return {
        name: {"label": report.label(name), "rows": _records(table.reset_index())}
        for name, table in report.tables.items()
    }


def _family(run: RunContext) -> str:
    experiment = registry.get_experiment(run.engine, run.run.experiment_id)
    hypothesis = registry.get_hypothesis(
        run.engine, experiment.hypothesis_id, experiment.hypothesis_version
    )
    return hypothesis.family_id


def _write_report(
    run: RunContext,
    summary: dict[str, Any],
    strategies: pd.DataFrame,
    forecasts: pd.DataFrame,
    returns: pd.DataFrame,
    evaluation: pd.DataFrame,
) -> Path:
    cfg = run.cfg
    reports = cfg.paths.resolve(cfg.paths.reports_dir)
    directory: Path = reports / REPORT_DIR / str(summary["dataset_id"]) / run.run_id
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        **summary,
        "strategies": _records(strategies),
        "forecasts": _records(forecasts),
    }
    json_path = directory / "board.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path = directory / "board.md"
    md_path.write_text(render_board(summary, strategies, forecasts), encoding="utf-8")
    returns_path = directory / "returns.parquet"
    returns.to_parquet(returns_path)
    evaluation_path = directory / "returns_evaluation.parquet"
    evaluation.to_parquet(evaluation_path)
    run.log_artifact(md_path, kind="baseline_board_report")
    run.log_artifact(json_path, kind="baseline_board")
    run.log_artifact(returns_path, kind=RETURNS_ARTIFACT)
    run.log_artifact(evaluation_path, kind=EVALUATION_RETURNS_ARTIFACT)
    return directory


def render_board(summary: dict[str, Any], strategies: pd.DataFrame, forecasts: pd.DataFrame) -> str:
    """The board as Markdown; every net figure is marked with the cost basis."""
    basis = summary["cost_basis"]
    trials = summary["trials"]
    review = " — **raw/effective above the review ratio: owner review**" if trials["review"] else ""
    timeframes = ", ".join(summary["signal_timeframes"]) or "none"
    lines = [
        f"# Baseline board — {summary['dataset_id']}",
        "",
        f"**Every net figure below is {basis}.**",
        "",
        f"- Run `{summary['run_id']}` "
        f"({'confirmatory' if summary['confirmatory'] else 'exploratory, not citable'}), "
        f"trial family `{summary['hypothesis_family']}`",
        f"- Base timeframe {summary['base_timeframe']}; rule signals on {timeframes} bars; "
        f"targets {', '.join(summary['targets'])}",
        f"- History: {summary['history_start']} to {summary['history_end']}, "
        f"{summary['history_days']} trading days. Rule baselines are evaluated over all of it "
        "(`full_history`), warmed up on signal bars from before its start where needed (C-29); "
        "forecast-sign baselines over the walk-forward test folds (`test_folds`)",
        f"- Out of sample: {summary['oos_start']} to {summary['oos_end']}, "
        f"{summary['oos_days']} trading days in {summary['folds']} walk-forward folds",
        f"- Cost model `{summary['cost_model']}` ({basis}); gates `{summary['gates_hash']}`; "
        f"board `{summary['board_hash']}`",
        f"- Daily net returns, annualized with {summary['periods_per_year']}; stationary "
        f"bootstrap ({summary['bootstrap']['n_boot']} resamples, block length "
        f"{summary['bootstrap']['block_length']}, at least "
        f"{summary['bootstrap']['min_block_days']} days); one-sided p-values",
        f"- Trials in the family: {trials['raw']} raw, {trials['effective']} effective; "
        f"DSR uses the {trials['gated']} count{review}",
        "",
        f"## Strategies — net of costs over the evaluation period ({basis})",
        "",
        "| Strategy | Target | Signal | Warm-up bars (pre-start) | Period | Evaluation start | "
        "Days | Sharpe [CI] | p(SR>0) | DSR | PSR | Annual return [CI] | Max drawdown [CI] | "
        "Trades | Random-entry p | Costs (USD) | Basis |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | "
        "--- | --- | --- |",
    ]
    records = _records(strategies)
    for row in records:
        costs = sum(
            row[c] for c in ("spread_cost", "slippage_cost", "commission", "financing") if row[c]
        )
        lines.append(
            f"| {row['strategy']} | {row['target'] or '—'} | {row['signal_timeframe'] or '—'} | "
            f"{_warmup(row)} | {row['period']} | {row['evaluation_start']} | "
            f"{row['evaluation_days']} | "
            f"{_f(row['sharpe'])} [{_f(row['sharpe_ci_low'])}, {_f(row['sharpe_ci_high'])}] | "
            f"{_f(row['sharpe_p'], 3)} | {_f(row['dsr'], 3)} | {_f(row['psr'], 3)} | "
            f"{_pct(row['annual_return'])} [{_pct(row['annual_return_ci_low'])}, "
            f"{_pct(row['annual_return_ci_high'])}] | {_pct(row['max_drawdown'])} "
            f"[{_pct(row['max_drawdown_ci_low'])}, {_pct(row['max_drawdown_ci_high'])}] | "
            f"{_f(row['trade_count'], 0)} | {_f(row['random_entry_p'], 3)} | {_f(costs, 0)} | "
            f"{row['cost_basis']} |"
        )
    lines += [
        "",
        f"## Fold-aligned view — descriptive comparison, no p-values ({basis})",
        "",
        "The same screens' daily net returns restricted to the out-of-sample days: identical days "
        "for every strategy and every later candidate. Not a separate screen and not a trial.",
        "",
        "| Strategy | Sharpe | Annual return | Net P&L (USD) | Basis |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in records:
        lines.append(
            f"| {row['strategy']} | {_f(row['fold_sharpe'])} | {_pct(row['fold_annual_return'])} "
            f"| {_f(row['fold_net_pnl'], 0)} | {row['cost_basis']} |"
        )
    lines += _render_slices(summary.get("slices"), basis)
    if len(forecasts):
        lines += [
            "",
            "## Forecast baselines — out-of-sample losses",
            "",
            "| Target | Baseline | Task | n | Mean loss [CI] | DM vs zero_return (one-sided p) |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in _records(forecasts):
            lines.append(
                f"| {row['target']} | {row['baseline']} | {row['task']} ({row['loss']}) | "
                f"{_f(row['n'], 0)} | {_e(row['mean_loss'])} [{_e(row['mean_loss_ci_low'])}, "
                f"{_e(row['mean_loss_ci_high'])}] | {_f(row['dm_vs_zero_p'], 3)} |"
            )
    lines += [
        "",
        "Baselines are benchmarks with parameters fixed in advance, never tuned (ADR 0033). "
        "Intervals are percentile intervals at the configured level.",
        "",
    ]
    return "\n".join(lines)


def _render_slices(slices: dict[str, Any] | None, basis: str) -> list[str]:
    """The descriptive slice tables (year by day, session by trade) and any slice not computed."""
    if not slices or (not slices["declared"] and slices["error"] is None):
        return []
    lines = ["", f"## Slices — {slices['label']} ({basis})", ""]
    if slices["error"] is not None:
        return [*lines, f"Not computed: {slices['error']}"]
    lines.append(f"Declared by {slices['hypothesis']}: {', '.join(slices['declared'])}.")
    errors = {n: t["error"] for n, t in slices["strategies"].items() if "error" in t}
    for name in slices["declared"]:
        by_trade = name == SESSION
        unit, measures = (
            ("Trades", ("trades", "mean_trade", "win_rate"))
            if by_trade
            else ("Days", ("days", "sharpe", "positive_days"))
        )
        lines += [
            "",
            f"### By {name} — descriptive",
            "",
            f"| Strategy | Bucket | {unit} | Net P&L (USD) | P&L share | "
            f"{'Mean trade (USD)' if by_trade else 'Sharpe'} | "
            f"{'Win rate' if by_trade else 'Positive days'} | Basis |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for strategy, tables in slices["strategies"].items():
            for row in tables.get(name, {}).get("rows", []):
                count, middle, share = (row.get(m) for m in measures)
                lines.append(
                    f"| {strategy} | {row['bucket']} | {_f(count, 0)} | "
                    f"{_f(row['net_pnl'], 0)} | {_pct(row['pnl_share'])} | "
                    f"{_f(middle)} | {_pct(share)} | {basis} |"
                )
    for strategy, message in errors.items():
        lines.append(f"\n{strategy}: slices not computed ({message})")
    return lines


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Rows as JSON-safe dicts (non-finite numbers become None)."""
    records = []
    for row in frame.to_dict(orient="records"):
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if value is pd.NA or (
                isinstance(value, float | np.floating) and not math.isfinite(float(value))
            ):
                clean[str(key)] = None
            elif isinstance(value, np.generic):
                clean[str(key)] = value.item()
            else:
                clean[str(key)] = value
        records.append(clean)
    return records


def _warmup(row: dict[str, Any]) -> str:
    """A rule's warm-up bars and, in parentheses, how many came from before the dataset."""
    if not row.get("warmup_bars"):
        return "—"
    return f"{row['warmup_bars']} ({row.get('pre_start_bars') or 0})"


def _f(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None or not math.isfinite(value) else f"{value:.{digits}f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None or not math.isfinite(value) else f"{100 * value:.1f}%"


def _e(value: float | None) -> str:
    return "n/a" if value is None or not math.isfinite(value) else f"{value:.3g}"


def _ns(index: pd.DatetimeIndex) -> npt.NDArray[np.int64]:
    values: npt.NDArray[np.int64] = (
        index.tz_convert("UTC").as_unit("ns").to_numpy(dtype="datetime64[ns]").view(np.int64)
    )
    return values
