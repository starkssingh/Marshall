"""The baseline board runner (BASE-005).

`run_baseline_board(run, dataset_id, board)` puts every baseline through the same walk-forward
folds, the same cost model and the same metrics, inside an experiment run:

1. **Folds.** The board's walk-forward splitter on the dataset's decision times, purged by the
   first target's ``label_end``. The out-of-sample (OOS) decisions are the union of the test
   windows; every strategy is judged on them and on every OOS trading day.
2. **Forecast baselines** (BASE-001, and BASE-003's ``ar1``) run through `run_walk_forward` for
   each target. The board reports their forecast metrics, a stationary-bootstrap interval of the
   mean loss, and a one-sided Diebold-Mariano test of beating ``zero_return`` (regression
   baselines; its horizon is the target horizon in base bars). Every forecast baseline except
   ``zero_return`` (always flat) also becomes a strategy: the sign of the forecast, or of
   ``p - 0.5`` for ``climatology``, re-decided at every decision.
3. **Rule baselines** (BASE-002) run on the signal bars of ``signal_timeframe`` (``1d``). When a
   ``vol_target`` is configured, each rule also runs volatility-targeted (``<name>_vol``), its
   realized volatility annualized with the signal bars per year (the gate periods per year for
   daily bars, times the bars in a regular trading day below that).
4. **Screening.** Each strategy's exposures on the OOS decisions go through the vectorized
   screener (BT-002) with the configured cost model, on the usable quotes of the dataset's source
   (loaded a month at a time and reduced to the quotes the screener can read,
   `required_quotes`), with slippage from the target set's sigma-hat of 1-minute returns.
5. **Metrics** are computed on daily net returns of every OOS trading day (0 before a strategy's
   first fill), under the gate conventions (VAL-007): annualized with
   ``cfg.gate_periods_per_year()``; stationary bootstrap with ``n_boot`` resamples and a
   Politis-White mean block length of at least ``min_block_days``; one-sided p-values. Sharpe,
   annual return, annual volatility, Sortino and maximum drawdown come with bootstrap intervals;
   the Sharpe ratio also with its three standard errors (VAL-001), the probabilistic Sharpe ratio,
   the minimum track record length and the random-entry null p-value (share of
   ``random_entry_seeds`` random-entry versions with a Sharpe ratio at least as high).
6. **Trials.** Every strategy is one trial of the run's hypothesis family, evaluated on test folds,
   with its annualized Sharpe ratio and daily returns (EXP-004); ``zero_return`` is not a strategy
   and random-entry draws are a reference distribution, not trials. After all of them are
   recorded, each strategy's deflated Sharpe ratio uses the family's gated trial count
   (``effective`` or ``raw``); the report shows both counts and flags a raw/effective ratio above
   the review ratio.
7. **Report** under ``reports/baselines/<dataset_id>/<run_id>/``: ``board.md``, ``board.json`` and
   ``returns.parquet`` (daily net returns per strategy, for paired tests against later
   candidates), each recorded as a run artifact. Every net figure carries the cost model's label —
   "screening, placeholder costs" while it is provisional (ADR 0032).
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

from xq.backtest.costs import CostModel
from xq.backtest.metrics import drawdown_metrics, return_metrics, trade_metrics
from xq.backtest.vectorized import BacktestResult, required_quotes, run_vectorized
from xq.core.config import AppConfig, GateConventions, gates_hash
from xq.core.errors import ConfigError, XQError
from xq.core.seeds import derive_seed
from xq.core.time import trading_day, trading_day_bounds, trading_days
from xq.core.types import Timeframe
from xq.data.calendar import NAT_NS, MarketClock, regular_trading_day
from xq.datasets.builder import load_dataset, read_manifest, usable_quotes
from xq.datasets.spec import DatasetSpec
from xq.models.base import ModelConfig
from xq.models.baselines import (
    RuleStrategyConfig,
    VolTargetConfig,
    forecast_baseline,
    positions_at,
    random_entry_null,
    random_walk_columns,
    rule_exposure,
    signal_bars,
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
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter
from xq.validation.walkforward import run_walk_forward

if TYPE_CHECKING:
    from xq.tracking.runs import RunContext

FloatArray = npt.NDArray[np.float64]
REPORT_DIR = "baselines"
REFERENCE_FORECAST = "zero_return"
_BPS = 1e4


class BoardError(XQError):
    """The board cannot be run on this dataset (no folds, a missing target or signal bars)."""


class BoardConfig(BaseModel):
    """The baseline board (``experiments/configs/baselines/board.yaml``), fixed in advance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    targets: list[str] = Field(min_length=1)
    walk_forward: WalkForwardConfig
    forecast_baselines: list[str] = []
    signal_timeframe: str = "1d"
    rules: dict[str, RuleStrategyConfig] = {}
    vol_target: VolTargetConfig | None = None
    random_entry_seeds: int = Field(default=1000, ge=0)
    ci_level: float = Field(default=0.95, gt=0, lt=1)

    @model_validator(mode="after")
    def _check(self) -> BoardConfig:
        for name in self.forecast_baselines:
            forecast_baseline(name)  # raises ConfigError for an unknown name
        return self

    def strategies(self) -> dict[str, RuleStrategyConfig]:
        """Every rule strategy: as configured, plus ``<name>_vol`` with a volatility target."""
        expanded = dict(self.rules)
        if self.vol_target is not None:
            for name, rule in self.rules.items():
                if not rule.vol_target:
                    expanded[f"{name}_vol"] = rule.model_copy(update={"vol_target": True})
        return expanded

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
    returns: pd.DataFrame
    report_dir: Path
    cost_basis: str
    summary: dict[str, Any]


@dataclass(frozen=True)
class _Strategy:
    name: str
    kind: str
    target: str | None
    params: dict[str, Any]
    positions: pd.Series


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
        BoardError: if the dataset yields no fold, lacks a target or the signal bars.
    """
    cfg = run.cfg
    conventions = cfg.gates_config().conventions
    periods = cfg.gate_periods_per_year()
    manifest = read_manifest(cfg, dataset_id)
    spec = DatasetSpec.model_validate(manifest["spec"])
    features = load_dataset(cfg, dataset_id, "features")
    target_frame = load_dataset(cfg, dataset_id, "targets")
    chosen = list(targets or board.targets)
    known = set(target_frame["target"].unique())
    missing = [t for t in chosen if t not in known]
    if missing:
        raise BoardError(f"dataset {dataset_id} has no targets {missing}")

    decisions = pd.DatetimeIndex(features.index)
    first = target_values(target_frame, chosen[0]).reindex(decisions)
    folds = WalkForwardSplitter(board.walk_forward).split(decisions, first["label_end"])
    if not folds:
        raise BoardError(f"the walk-forward splitter gives no fold on dataset {dataset_id}")
    oos = decisions[np.unique(np.concatenate([f.test_idx for f in folds]))]
    oos_days = sorted({d.item() for d in trading_days(oos)})

    costs = CostModel.from_config(cfg, spec.instrument, engine=run.engine, source_id=spec.source)
    clock = MarketClock.for_range(
        cfg.sessions_config(),
        trading_day(oos[0]) - timedelta(days=1),
        trading_day(oos[-1]) + timedelta(days=10),
    )
    excluded = {date.fromisoformat(e["trading_day"]) for e in manifest["excluded_partitions"]}
    quotes = _screening_quotes(cfg, spec, oos, excluded, costs, clock)
    sigma = _sigma_1m_bps(cfg, spec, features).reindex(oos)

    strategies: list[_Strategy] = []
    forecast_rows: list[dict[str, Any]] = []
    for target in chosen:
        rows, made = _forecast_baselines(
            run, dataset_id, spec, features, target, board, oos, conventions, n_jobs, use_cache
        )
        forecast_rows.extend(rows)
        strategies.extend(made)
    strategies.extend(_rule_strategies(cfg, features, board, oos, periods))

    family = _family(run)
    capital = cfg.backtest_config().capital_usd
    evaluated: list[tuple[_Strategy, BacktestResult, pd.Series]] = []
    for strategy in strategies:
        result = run_vectorized(
            strategy.positions, quotes, costs, clock, capital=capital, sigma_1m_bps=sigma
        )
        returns = _daily_returns(result, oos_days)
        annual_sharpe = return_metrics(returns, periods)["sharpe"]
        run.record_trial(
            family_id=family,
            config={
                "board": board.config_hash(),
                "dataset_id": dataset_id,
                "strategy": strategy.name,
                "kind": strategy.kind,
                "target": strategy.target,
                "params": strategy.params,
            },
            evaluated_on_test=True,
            sharpe=annual_sharpe if math.isfinite(annual_sharpe) else None,
            returns=_trial_returns(returns),
        )
        evaluated.append((strategy, result, returns))

    stats = trial_count(cfg, run.engine, family)
    use_effective = conventions.trial_count == "effective"
    rows = []
    for strategy, result, returns in evaluated:
        seed = derive_seed(run.run.seed, "baseline_board", strategy.name)
        row: dict[str, Any] = {
            "strategy": strategy.name,
            "kind": strategy.kind,
            "target": strategy.target,
            "cost_basis": result.cost_basis,
            **_strategy_metrics(
                returns, result, oos_days, periods, conventions, board.ci_level, seed
            ),
        }
        row["dsr"] = _dsr(cfg, run, family, returns, periods, use_effective)
        row["random_entry_p"] = _random_entry_p(
            strategy.positions,
            sharpe_ratio(returns.to_numpy(np.float64)),
            quotes,
            costs,
            clock,
            capital,
            sigma,
            oos_days,
            board,
            seed,
        )
        rows.append(row)

    strategy_frame = pd.DataFrame(rows)
    forecast_frame = pd.DataFrame(forecast_rows)
    returns_frame = pd.DataFrame(
        {s.name: r.to_numpy() for s, _, r in evaluated},
        index=pd.Index([d.isoformat() for d in oos_days], name="trading_day"),
    )
    summary = {
        "dataset_id": dataset_id,
        "run_id": run.run_id,
        "confirmatory": run.confirmatory,
        "hypothesis_family": family,
        "cost_model": cfg.backtest_config().cost_model,
        "cost_basis": costs.result_label,
        "gates_hash": gates_hash(cfg.gates_config()),
        "board_hash": board.config_hash(),
        "base_timeframe": spec.base_timeframe.value,
        "signal_timeframe": board.signal_timeframe,
        "targets": chosen,
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
    }
    report_dir = _write_report(run, summary, strategy_frame, forecast_frame, returns_frame)
    for row in rows:
        for key in ("sharpe", "sharpe_ci_low", "sharpe_ci_high", "sharpe_p", "dsr", "trade_count"):
            value = row.get(key)
            if value is not None and math.isfinite(value):
                run.log_metric(f"board/{row['strategy']}/{key}", float(value))
    return BoardResult(
        strategy_frame, forecast_frame, returns_frame, report_dir, costs.result_label, summary
    )


def _forecast_baselines(
    run: RunContext,
    dataset_id: str,
    spec: DatasetSpec,
    features: pd.DataFrame,
    target: str,
    board: BoardConfig,
    oos: pd.DatetimeIndex,
    conventions: GateConventions,
    n_jobs: int,
    use_cache: bool,
) -> tuple[list[dict[str, Any]], list[_Strategy]]:
    """Forecast rows and forecast-sign strategies of every forecast baseline on one target."""
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
                _Strategy(f"{name}:{target}", "forecast_sign", target, params, positions)
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
    cfg: AppConfig,
    features: pd.DataFrame,
    board: BoardConfig,
    oos: pd.DatetimeIndex,
    periods: int,
) -> list[_Strategy]:
    if not board.rules:
        return []
    # volatility targeting annualizes signal-bar returns: one bar a day for 1d, more below it
    timeframe = Timeframe(board.signal_timeframe)
    if timeframe is not Timeframe.D1:
        bars_per_day = regular_trading_day(cfg.sessions_config()) / timeframe.duration
        periods = round(periods * bars_per_day)
    prefix = f"ctx_{board.signal_timeframe}_"
    try:
        bars = signal_bars(features, prefix)
    except KeyError as exc:
        raise BoardError(
            f"the dataset has no {board.signal_timeframe} context bars for the rule baselines"
        ) from exc
    strategies = []
    for name, rule in board.strategies().items():
        exposure = rule_exposure(bars, rule, vol_target=board.vol_target, periods_per_year=periods)
        kind = "rule_vol" if rule.vol_target else "rule"
        params = {"rule": rule.rule, **rule.params}
        strategies.append(_Strategy(name, kind, None, params, positions_at(oos, exposure)))
    return strategies


def _screening_quotes(
    cfg: AppConfig,
    spec: DatasetSpec,
    decisions: pd.DatetimeIndex,
    excluded: set[date],
    costs: CostModel,
    clock: MarketClock,
) -> pd.DataFrame:
    """The usable quotes a screen of `decisions` reads, loaded a month of trading days at a time."""
    days = trading_days(decisions)
    months = np.array([d.item().strftime("%Y-%m") for d in days])
    vault = pd.Timestamp(cfg.vault.start)
    parts = []
    for month in np.unique(months):
        chunk = decisions[months == month]
        start = trading_day_bounds(trading_day(chunk[0]))[0]
        end = trading_day_bounds(trading_day(chunk[-1]))[1]
        intended = clock.advance(_ns(chunk), costs.latency.value)
        intended = intended[intended != NAT_NS]
        if len(intended):
            reach = pd.Timestamp(int(intended.max()), tz="UTC") + costs.max_fill_delay
            end = max(end, reach + pd.Timedelta(1, "ns"))
        ticks = usable_quotes(cfg, spec, start, min(end, vault), excluded)
        parts.append(ticks.iloc[required_quotes(ticks, chunk, costs, clock)])
    quotes = pd.concat(parts).drop_duplicates(subset=["raw_file_id", "row_num"])
    quotes = quotes.sort_values(["ts_utc", "raw_file_id", "row_num"], kind="stable")
    return quotes.loc[:, ["ts_utc", "bid", "ask"]].reset_index(drop=True)


def _sigma_1m_bps(cfg: AppConfig, spec: DatasetSpec, features: pd.DataFrame) -> pd.Series:
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


def _daily_returns(result: BacktestResult, days: list[date]) -> pd.Series:
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
    metrics.update(drawdown_metrics(pd.Series(result.capital * (1 + np.cumsum(r)))))
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
    equity = 1 + np.cumsum(draws, axis=1)
    peak = np.maximum.accumulate(equity, axis=1)
    result: FloatArray = np.max((peak - equity) / peak, axis=1)
    return result


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
    quotes: pd.DataFrame,
    costs: CostModel,
    clock: MarketClock,
    capital: float,
    sigma: pd.Series,
    days: list[date],
    board: BoardConfig,
    seed: int,
) -> float:
    """Share of random-entry versions with a Sharpe ratio at least the strategy's (plus one)."""
    if board.random_entry_seeds == 0 or not (positions != 0).any() or not math.isfinite(observed):
        return math.nan
    null = []
    for draw in random_entry_null(positions, board.random_entry_seeds, seed=seed):
        screened = run_vectorized(draw, quotes, costs, clock, capital=capital, sigma_1m_bps=sigma)
        null.append(sharpe_ratio(_daily_returns(screened, days).to_numpy(np.float64)))
    values = np.array(null, dtype=np.float64)
    values = values[np.isfinite(values)]
    return (1 + int(np.sum(values >= observed))) / (1 + len(values))


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
    run.log_artifact(md_path, kind="baseline_board_report")
    run.log_artifact(json_path, kind="baseline_board")
    run.log_artifact(returns_path, kind="baseline_returns")
    return directory


def render_board(summary: dict[str, Any], strategies: pd.DataFrame, forecasts: pd.DataFrame) -> str:
    """The board as Markdown; every net figure is marked with the cost basis."""
    basis = summary["cost_basis"]
    trials = summary["trials"]
    review = " — **raw/effective above the review ratio: owner review**" if trials["review"] else ""
    lines = [
        f"# Baseline board — {summary['dataset_id']}",
        "",
        f"**Every net figure below is {basis}.**",
        "",
        f"- Run `{summary['run_id']}` "
        f"({'confirmatory' if summary['confirmatory'] else 'exploratory, not citable'}), "
        f"trial family `{summary['hypothesis_family']}`",
        f"- Base timeframe {summary['base_timeframe']}; rule signals on "
        f"{summary['signal_timeframe']} bars; targets {', '.join(summary['targets'])}",
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
        f"## Strategies — net of costs ({basis})",
        "",
        "| Strategy | Target | Sharpe [CI] | p(SR>0) | DSR | PSR | Annual return [CI] | "
        "Max drawdown [CI] | Trades | Random-entry p | Costs (USD) | Basis |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in _records(strategies):
        costs = sum(
            row[c] for c in ("spread_cost", "slippage_cost", "commission", "financing") if row[c]
        )
        lines.append(
            f"| {row['strategy']} | {row['target'] or '—'} | "
            f"{_f(row['sharpe'])} [{_f(row['sharpe_ci_low'])}, {_f(row['sharpe_ci_high'])}] | "
            f"{_f(row['sharpe_p'], 3)} | {_f(row['dsr'], 3)} | {_f(row['psr'], 3)} | "
            f"{_pct(row['annual_return'])} [{_pct(row['annual_return_ci_low'])}, "
            f"{_pct(row['annual_return_ci_high'])}] | {_pct(row['max_drawdown'])} "
            f"[{_pct(row['max_drawdown_ci_low'])}, {_pct(row['max_drawdown_ci_high'])}] | "
            f"{_f(row['trade_count'], 0)} | {_f(row['random_entry_p'], 3)} | {_f(costs, 0)} | "
            f"{row['cost_basis']} |"
        )
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


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Rows as JSON-safe dicts (non-finite numbers become None)."""
    records = []
    for row in frame.to_dict(orient="records"):
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, float | np.floating) and not math.isfinite(float(value)):
                clean[str(key)] = None
            elif isinstance(value, np.generic):
                clean[str(key)] = value.item()
            else:
                clean[str(key)] = value
        records.append(clean)
    return records


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
