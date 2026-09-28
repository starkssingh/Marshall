"""Subject adapters for recorded runs (ROB-008, `xq validate-strategy`; ADR 0058).

A subject adapter rebuilds, from a recorded run, the strategy it chose as a `StrategySubject`:
its daily net returns, closed trades and folds, the family it was selected from, and functions
that re-evaluate it at other parameters, costs, delays and noise. The simulated strategies'
adapter lives with them (`xq.robustness.simulated`); this module holds the adapters of real
research runs.

**Baseline board runs** (``baseline_board``, `board_subject`). A board run evaluates many
strategies; one is validated at a time (``--strategy``).

1. **The screening context is rebuilt** from the run's recorded configuration (the board and its
   targets) and dataset, by the code the board ran (`xq.models.board.screening_context`): the
   same walk-forward folds and out-of-sample decisions, quotes, cost model, market clock and
   sigma-hat. A dataset whose directory is gone is rebuilt from its recorded spec, and must build
   the same dataset id. The quotes also cover ROB-002's latency stress.
2. **The strategy is rebuilt and checked.** A rule baseline (``rule`` or ``rule_vol``) is
   recomputed from the dataset's signal bars. A forecast-sign strategy is rebuilt from the
   predictions the run stored (verified against their recorded SHA-256). Its daily net returns
   on every out-of-sample day must equal the ones the run recorded (``returns.parquet``, also
   verified), within ``1e-9 + 1e-6 |x|``; otherwise the code or the data changed and the run is
   refused: reproduce it first (`xq exp reproduce`).
3. **The family** is every strategy of the board, with its recorded daily returns. The
   **baselines** of R1 are the board's other strategies: a baseline validated as a candidate must
   beat the best of the others.
4. **Parameters** (C-25, ADR 0057). Board strategies have no tuned parameter. A rule's numeric
   constants (its ``params``, and the volatility target of a ``_vol`` rule) are perturbed, unless
   the hypothesis declares them fixed a priori with a source. A perturbed point that the rule
   refuses (for example a fast moving average no longer faster than the slow one) never trades:
   it counts as not profitable, the conservative direction. A forecast-sign strategy's model is
   not refitted at perturbed constants, so its neighbourhood is **not evaluated** (the verdict is
   incomplete) unless its parameters are declared fixed a priori.
5. **Re-evaluation.** Delays shift the target positions by whole decision bars (ROB-007). Price
   noise disturbs a rule's signal bars by multiples of the median quoted spread; the fills stay
   on the true quotes (ROB-005). Rules read no other feature, and forecast-sign strategies would
   need their models refitted on noisy inputs, so those kinds of noise are not applicable.
6. **Trades** are the screen's closed holding episodes. ``trade_return`` is the episode's net
   P&L over its notional at entry, and ``sigma_daily`` the sigma-hat of 1-minute returns at the
   entry decision scaled to a regular trading day. Episodes entered before any sigma-hat is known
   are left out of the trade list (they stay in the returns).

Net figures carry the cost model's label ("screening, placeholder costs" while it is
provisional).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from sqlalchemy import Engine

from xq.backtest.vectorized import BacktestResult
from xq.core.config import AppConfig, GatesConfig
from xq.core.errors import ConfigError, XQError
from xq.core.ids import git_sha
from xq.core.time import trading_days
from xq.data.calendar import regular_trading_day
from xq.data.raw_store import sha256_file
from xq.datasets.builder import (
    NoDatasetDataError,
    build_dataset,
    read_manifest,
    recorded_spec,
)
from xq.models.baselines import VolTargetConfig, forecast_baseline
from xq.models.board import (
    BoardConfig,
    ScreeningContext,
    rule_positions,
    rule_signal_bars,
    screening_context,
)
from xq.robustness.costs_stress import CostStressResult, cost_stress, plan_scenarios
from xq.robustness.delay import delayed_positions
from xq.robustness.noise import NoiseKind, NoisyEvaluate, noisy_prices
from xq.robustness.perturb import Parameter, config_constants, with_constants
from xq.robustness.slicing import DeclaredSlices
from xq.robustness.subject import TRADE_COLUMNS, StrategySubject, TrialSummary
from xq.tracking import registry
from xq.tracking.registry import RunRef
from xq.validation.predictions import prediction_file_name, read_predictions

FloatArray = npt.NDArray[np.float64]
BOARD_KIND = "baseline_board"
RETURNS_ARTIFACT = "baseline_returns"
PREDICTIONS_ARTIFACT = "predictions"
#: Tolerance of the rebuilt daily returns against the recorded ones.
RTOL, ATOL = 1e-6, 1e-9
PRICE_COLUMNS = ("open", "high", "low", "close")
_BPS = 1e4


class StrategyValidationError(XQError):
    """A run cannot be validated (unfinished, a kind without a subject adapter, a strategy it did
    not evaluate, or a rebuild that does not match what it recorded)."""


def board_subject(
    cfg: AppConfig,
    engine: Engine,
    run: RunRef,
    strategy: str | None,
    *,
    trials: TrialSummary,
    slices: DeclaredSlices | None,
    parameters_fixed_a_priori: str | None,
) -> StrategySubject:
    """The strategy `strategy` of baseline board run `run` as a validation subject (module
    docstring).

    Raises:
        StrategyValidationError: for no or an unknown strategy, a dataset that no longer builds
            the same id, altered artifacts, or rebuilt returns that differ from the recorded ones.
    """
    if run.dataset_id is None:
        raise StrategyValidationError(f"board run {run.run_id} records no dataset")
    config = run.config["run"]
    board = BoardConfig.model_validate(config["board"])
    targets = [str(t) for t in config["targets"]]
    recorded = _recorded_returns(engine, run)
    if strategy is None:
        raise StrategyValidationError(
            f"board run {run.run_id} holds {recorded.shape[1]} strategies; choose one with "
            f"--strategy ({', '.join(recorded.columns)})"
        )
    if strategy not in recorded.columns:
        raise StrategyValidationError(f"board run {run.run_id} has no strategy {strategy!r}")
    _ensure_dataset(cfg, engine, run.dataset_id)
    gates = cfg.gates_config()
    latencies = sorted({s.extra_latency_ms for s in plan_scenarios(gates) if s.extra_latency_ms})
    context = screening_context(
        cfg, engine, run.dataset_id, board, targets, extra_latencies_ms=latencies
    )
    if [d.isoformat() for d in context.oos_days] != list(recorded.index):
        raise StrategyValidationError(
            f"the rebuilt out-of-sample days of run {run.run_id} differ from the recorded ones"
        )
    days = pd.Index(context.oos_days, name="trading_day")
    family = pd.DataFrame(recorded.to_numpy(np.float64), index=days, columns=recorded.columns)

    rebuild = _rule(cfg, board, strategy, context) or _forecast(
        engine, run, board, targets, strategy, context
    )
    if rebuild is None:
        raise StrategyValidationError(
            f"board run {run.run_id}: {strategy!r} is neither a rule of its board nor a "
            "forecast-sign strategy of its targets"
        )
    positions = rebuild.positions({})
    result = context.screen(positions)
    returns = context.daily_returns(result).rename("return")
    expected = family[strategy].to_numpy(np.float64)
    if not np.allclose(returns.to_numpy(np.float64), expected, rtol=RTOL, atol=ATOL):
        worst = float(np.max(np.abs(returns.to_numpy(np.float64) - expected)))
        raise StrategyValidationError(
            f"the rebuilt daily returns of {strategy!r} differ from those run {run.run_id} "
            f"recorded (largest difference {worst:.3g}): the code or the data changed; "
            "reproduce the run first (xq exp reproduce)"
        )
    returns.index = days

    def evaluate(values: Mapping[str, float]) -> FloatArray:
        try:
            moved = rebuild.positions(values)
        except ConfigError:
            return np.zeros(len(days))  # a refused point never trades: not profitable
        return _returns(context, moved)

    def stress(gates: GatesConfig) -> CostStressResult:
        return cost_stress(
            positions,
            context.quotes,
            context.costs,
            context.clock,
            capital=context.capital,
            periods_per_year=context.periods_per_year,
            gates=gates,
            sigma_1m_bps=context.sigma,
        )

    minutes = regular_trading_day(cfg.sessions_config()) / pd.Timedelta(minutes=1)
    sigma_daily = _day_sigma(context, minutes)
    others = family.drop(columns=[strategy])
    return StrategySubject(
        name=strategy,
        source=f"baseline board run {run.run_id}, strategy {strategy}",
        synthetic=False,
        cost_basis=result.cost_basis,
        capital=context.capital,
        periods_per_year=context.periods_per_year,
        returns=returns,
        trades=_trades(result, context, minutes),
        family=family,
        folds=context.day_folds().set_axis(days),
        trials=trials,
        parameters=rebuild.parameters,
        evaluate=evaluate if rebuild.parameters else None,
        cost_stress=stress,
        delayed=lambda bars: _returns(context, delayed_positions(positions, bars)),
        noisy=rebuild.noisy,
        noise_not_applicable=rebuild.noise_not_applicable,
        baselines=others if others.shape[1] else None,
        sigma_daily=sigma_daily,
        slices=slices,
        sessions=cfg.sessions_config(),
        parameter_kind="constants",
        held_constants=rebuild.held,
        parameters_fixed_a_priori=parameters_fixed_a_priori,
        neighbourhood_unavailable=rebuild.unavailable,
    )


class _Rebuild:
    """How to recompute one board strategy's positions, and what can be varied."""

    def __init__(
        self,
        positions: Callable[[Mapping[str, float]], pd.Series],
        *,
        parameters: tuple[Parameter, ...] = (),
        held: tuple[str, ...] = (),
        noisy: dict[NoiseKind, NoisyEvaluate] | None = None,
        noise_not_applicable: dict[NoiseKind, str] | None = None,
        unavailable: str | None = None,
    ) -> None:
        self.positions = positions
        self.parameters = parameters
        self.held = held
        self.noisy = noisy or {}
        self.noise_not_applicable = noise_not_applicable or {}
        self.unavailable = unavailable


def _rule(
    cfg: AppConfig, board: BoardConfig, strategy: str, context: ScreeningContext
) -> _Rebuild | None:
    rules = board.strategies()
    if strategy not in rules:
        return None
    rule = rules[strategy]
    bars, bar_periods = rule_signal_bars(cfg, context.features, board, context.periods_per_year)
    constants: dict[str, Any] = {"params": dict(rule.params)}
    if rule.vol_target and board.vol_target is not None:
        constants["vol_target"] = board.vol_target.model_dump(mode="json")
    parameters, held = config_constants(constants)

    def positions(values: Mapping[str, float], signal: pd.DataFrame = bars) -> pd.Series:
        moved = with_constants(constants, values)
        target = (
            VolTargetConfig.model_validate(moved["vol_target"])
            if "vol_target" in moved
            else board.vol_target
        )
        changed = rule.model_copy(update={"params": moved["params"]})
        return rule_positions(signal, changed, target, bar_periods, context.oos)

    quotes = context.quotes
    spread = float(np.median((quotes["ask"] - quotes["bid"]).to_numpy(np.float64)))

    def noisy(level: float, seed: int) -> FloatArray:
        disturbed = bars.copy()
        columns = list(PRICE_COLUMNS)
        disturbed[columns] = noisy_prices(bars[columns], spread, level, seed)
        return _returns(context, positions({}, disturbed))

    return _Rebuild(
        positions,
        parameters=parameters,
        held=held,
        noisy={"price": noisy},
        noise_not_applicable={"feature": "the rule reads only its signal bars' prices"},
    )


def _forecast(
    engine: Engine,
    run: RunRef,
    board: BoardConfig,
    targets: list[str],
    strategy: str,
    context: ScreeningContext,
) -> _Rebuild | None:
    name, sep, target = strategy.partition(":")
    if not sep or name not in board.forecast_baselines or target not in targets:
        return None
    path = _artifact(engine, run, PREDICTIONS_ARTIFACT, prediction_file_name(strategy))
    predictions = read_predictions(path)
    classification = forecast_baseline(name).task == "classification"
    raw = predictions["p_raw"] - 0.5 if classification else predictions["y_pred"]
    signs = np.sign(raw.reindex(context.oos).to_numpy(np.float64))
    fixed = pd.Series(np.nan_to_num(signs, nan=0.0), index=context.oos, name="exposure")
    reason = "the forecast model would have to be refitted on disturbed inputs"
    return _Rebuild(
        lambda values: fixed,
        noise_not_applicable={"price": reason, "feature": reason},
        unavailable=(
            "the adapter does not refit a forecast model at perturbed constants; declare its "
            "parameters fixed a priori with a source, or validate it from a model run"
        ),
    )


def _returns(context: ScreeningContext, positions: pd.Series) -> FloatArray:
    values: FloatArray = context.daily_returns(context.screen(positions)).to_numpy(np.float64)
    return values


def _trades(result: BacktestResult, context: ScreeningContext, minutes: float) -> pd.DataFrame:
    """Closed episodes in `TRADE_COLUMNS` (module docstring, item 6)."""
    closed = result.trades.loc[~result.trades["open"].astype(bool)]
    fills = result.fills
    fill_times = pd.DatetimeIndex(fills["fill_time"])
    sigma = context.sigma
    rows = []
    for entry, exit_, side, pnl in zip(
        pd.DatetimeIndex(closed["entry_time"]),
        pd.DatetimeIndex(closed["exit_time"]),
        closed["side"].to_numpy(np.float64),
        closed["pnl"].to_numpy(np.float64),
        strict=True,
    ):
        at = np.flatnonzero(fill_times == entry)
        if not len(at):
            continue
        fill = fills.iloc[int(at[-1])]  # the fill that opened the episode (after any close)
        sigma_1m = float(sigma.get(fill["decision_time"], math.nan))
        if not math.isfinite(sigma_1m):
            continue
        price = float(fill["price"])
        notional = abs(float(fill["position_lots"])) * result.contract_size * price
        rows.append(
            {
                "entry_time": entry,
                "exit_time": exit_,
                "direction": int(side),
                "entry_price": price,
                "spread": float(fill["ask"]) - float(fill["bid"]),
                "sigma_daily": sigma_1m / _BPS * math.sqrt(minutes),
                "trade_return": float(pnl) / notional,
                "pnl": float(pnl),
            }
        )
    return pd.DataFrame(rows, columns=list(TRADE_COLUMNS))


def _day_sigma(context: ScreeningContext, minutes: float) -> pd.Series:
    """Daily sigma-hat known at each OOS day's first decision (for volatility-tercile slices)."""
    frame = pd.DataFrame(
        {
            "day": [d.item() for d in trading_days(context.oos)],
            "sigma": context.sigma.to_numpy(np.float64) / _BPS * math.sqrt(minutes),
        }
    )
    first = frame.groupby("day", sort=True)["sigma"].first()
    return first.reindex(context.oos_days).rename("sigma_daily")


def _recorded_returns(engine: Engine, run: RunRef) -> pd.DataFrame:
    path = _artifact(engine, run, RETURNS_ARTIFACT, None)
    frame = pd.read_parquet(path)
    frame.index = [str(i) for i in frame.index]
    return frame


def _artifact(engine: Engine, run: RunRef, kind: str, name: str | None) -> Path:
    """The path of a run's artifact of `kind` (named `name`), after checking its SHA-256."""
    matches = [
        a
        for a in registry.list_artifacts(engine, run.run_id)
        if a.kind == kind and (name is None or Path(a.path).name == name)
    ]
    if len(matches) != 1:
        raise StrategyValidationError(
            f"run {run.run_id} has {len(matches)} {kind} artifact(s)"
            + (f" named {name}" if name else "")
            + "; expected one"
        )
    path = Path(matches[0].path)
    if not path.is_file() or sha256_file(path) != matches[0].sha256:
        raise StrategyValidationError(
            f"artifact {path} of run {run.run_id} is missing or altered since it was recorded"
        )
    return path


def _ensure_dataset(cfg: AppConfig, engine: Engine, dataset_id: str) -> None:
    """Rebuild a dataset whose directory is gone from its recorded spec (same id required)."""
    try:
        read_manifest(cfg, dataset_id)
    except NoDatasetDataError:
        repo = cfg.paths.resolve(cfg.paths.root)
        rebuilt = build_dataset(
            cfg, engine, recorded_spec(engine, dataset_id), git_sha=git_sha(repo)
        )
        if rebuilt.dataset_id != dataset_id:
            raise StrategyValidationError(
                f"the recorded spec of {dataset_id} now builds {rebuilt.dataset_id}: the code "
                "that produces the dataset changed"
            ) from None
