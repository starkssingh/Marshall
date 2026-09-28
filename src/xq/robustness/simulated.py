"""Simulated strategies with known truth, as validation subjects (ROB-008; ADR 0054, ADR 0056).

**Synthetic data only.** These strategies exist to prove that the validation and robustness
reports tell a genuine edge from an overfit one before any real candidate is judged by them.
Their results are engineering checks, never evidence about XAUUSD.

Both truths trade one synthetic asset on daily bars (20 years of them by default). Positions (-1,
0 or +1 times the capital) are held through a trading day and changed at its start. Net P&L over
the capital is

    position x return - cost_per_turnover x |change of position| - financing x |position|

The cost per unit of turnover is the half-spread plus slippage plus commission. A strategy's
**family** is the grid of configurations tried, and the **candidate** is the one with the best
net Sharpe ratio over the evaluated days: the selection the deflated Sharpe ratio, PBO and SPA
must correct for.

- **genuine**: returns carry a slowly varying drift (a persistent AR(1) mean) plus noise. The
  strategy is a trend rule: the sign of the t-statistic of the mean of the last ``lookback``
  returns, flat while it lies within +/- ``deadband``. Grid: eight lookbacks from 2 to 80 days
  times three deadbands (24 configurations). Lookbacks of a few days see mostly noise and pay for
  their turnover, so the family has weaker members and the in-sample choice carries information,
  as in a real search. The edge is real, survives nearby parameters and decays smoothly with
  delay.
- **overfit**: returns are pure noise. The strategy at a parameter point ``(a, b)`` holds random
  +/-1 positions for geometric spells of mean ``a`` days, drawn from a generator seeded by the
  point, so every configuration is independent noise. Grid: ``a`` in 5 ... 14 and ``b`` in
  1 ... 5 (50 configurations). The in-sample winner is a single-point optimum on noise.

Their baseline is buy-and-hold on the same asset with the same costs (R1: beat the best baseline).
A `SimulationSpec` fixes everything, so a simulated run can be rebuilt exactly from its recorded
configuration (`simulated_subject`).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from datetime import date
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from xq.backtest.metrics import return_metrics
from xq.core.config import GatesConfig, SessionsConfig
from xq.core.seeds import make_rng
from xq.core.time import trading_day_bounds
from xq.robustness.costs_stress import GATE_SCENARIO, CostScenario, CostStressResult
from xq.robustness.noise import NoiseKind, NoisyEvaluate, noisy_features, noisy_prices
from xq.robustness.perturb import Parameter
from xq.robustness.slicing import DeclaredSlices
from xq.robustness.subject import TRADE_COLUMNS, StrategySubject, TrialSummary
from xq.validation.sharpe import sharpe_ratio

FloatArray = npt.NDArray[np.float64]
Truth = Literal["genuine", "overfit"]
_BPS = 1e-4
GENUINE_LOOKBACKS = (2, 3, 5, 10, 20, 40, 60, 80)
GENUINE_DEADBANDS = (0.25, 0.5, 1.0)
OVERFIT_A = tuple(float(a) for a in range(5, 15))
OVERFIT_B = (1.0, 2.0, 3.0, 4.0, 5.0)
N_FOLDS = 10
SIGMA_SPAN = 20
#: The genuine edge's drift: an AR(1) mean with this persistence and standard deviation. A
#: typical candidate's net Sharpe ratio is 1.0 - 1.7 a year over 20 years: plausible, not a leak.
GENUINE_PERSISTENCE = 0.99
GENUINE_DRIFT_SIGMA = 0.0006
DAILY_NOISE = 0.004


class SimulationSpec(BaseModel):
    """Everything that defines a simulated strategy (module docstring)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    truth: Truth
    seed: int = Field(ge=0, lt=2**32)
    days: int = Field(default=5040, ge=300)
    first_day: date = date(2012, 1, 3)
    price: float = Field(default=2000.0, gt=0)
    #: Full spread in basis points of the price; a unit of turnover pays half of it.
    spread_bps: float = Field(default=1.5, ge=0)
    slippage_bps: float = Field(default=0.5, ge=0)
    commission_bps: float = Field(default=0.35, ge=0)
    #: Financing charged per day on the position held, in basis points.
    financing_bps_per_day: float = Field(default=0.15, ge=0)


class _Market:
    """The synthetic asset of a spec: returns, prices and the causal daily sigma-hat."""

    def __init__(self, spec: SimulationSpec) -> None:
        self.spec = spec
        rng = make_rng(spec.seed)
        n = spec.days
        noise = rng.normal(0.0, DAILY_NOISE, n)
        if spec.truth == "genuine":
            persistence, drift_sigma = GENUINE_PERSISTENCE, GENUINE_DRIFT_SIGMA
            shocks = rng.normal(0.0, drift_sigma * math.sqrt(1 - persistence**2), n)
            level = rng.normal(0.0, drift_sigma)
            mu = np.empty(n)
            for t in range(n):
                level = persistence * level + shocks[t]
                mu[t] = level
            self.returns: FloatArray = mu + noise
        else:
            self.returns = noise
        self.days = pd.Index(pd.bdate_range(spec.first_day, periods=n).date, name="trading_day")
        #: Price at each day's start (the close of the day before).
        self.open_prices: FloatArray = spec.price * np.exp(
            np.concatenate([[0.0], np.cumsum(self.returns)[:-1]])
        )
        r = pd.Series(self.returns)
        self.sigma: FloatArray = (
            r.ewm(span=SIGMA_SPAN, min_periods=SIGMA_SPAN // 2).std().shift(1).to_numpy()
        )
        s = spec
        self.cost_per_turnover = (s.spread_bps / 2 + s.slippage_bps + s.commission_bps) * _BPS
        self.financing = s.financing_bps_per_day * _BPS

    def pnl(
        self,
        positions: FloatArray,
        *,
        spread: float = 1.0,
        slippage: float = 1.0,
        commission: float = 1.0,
        financing: float = 1.0,
    ) -> FloatArray:
        """Daily net P&L over capital of `positions`, with every cost multiplied."""
        s = self.spec
        per_turnover = (
            spread * s.spread_bps / 2 + slippage * s.slippage_bps + commission * s.commission_bps
        ) * _BPS
        turnover = np.abs(np.diff(positions, prepend=0.0))
        held = np.abs(positions)
        result: FloatArray = (
            positions * self.returns - per_turnover * turnover - financing * self.financing * held
        )
        return result


def _trend_t_stat(returns: FloatArray, lookback: int) -> FloatArray:
    """The t-statistic of the mean of the previous `lookback` returns, known at each day's start."""
    window = pd.Series(returns).rolling(int(lookback))
    mean = window.mean().shift(1).to_numpy(np.float64)
    std = window.std().shift(1).to_numpy(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        t_stat: FloatArray = mean / (std / math.sqrt(lookback))
    return t_stat


def _from_t_stat(t_stat: FloatArray, deadband: float) -> FloatArray:
    positions: FloatArray = np.where(np.abs(np.nan_to_num(t_stat)) > deadband, np.sign(t_stat), 0.0)
    return np.nan_to_num(positions)


def _overfit_positions(spec: SimulationSpec, a: float, b: float) -> FloatArray:
    """Random +/-1 spells of mean `a` days from a generator seeded by the point: pure noise."""
    digest = hashlib.sha256(repr((spec.seed, float(a), float(b))).encode()).hexdigest()
    rng = np.random.default_rng(int(digest[:12], 16))
    switch = rng.random(spec.days) < 1 / max(a, 1.0)
    switch[0] = True
    draws = rng.choice([-1.0, 1.0], spec.days)
    regime = np.maximum.accumulate(np.where(switch, np.arange(spec.days), 0))
    positions: FloatArray = draws[regime]
    return positions


def _delayed(positions: FloatArray, bars: int) -> FloatArray:
    held = np.roll(positions, bars)
    if bars:
        held[:bars] = 0.0
    return held


class _Strategy:
    """Positions of the spec's rule at a parameter point, optionally from disturbed inputs."""

    def __init__(self, market: _Market) -> None:
        self.market = market
        self.spec = market.spec

    def positions(
        self,
        values: Mapping[str, float],
        *,
        price_noise: tuple[float, int] | None = None,
        feature_noise: tuple[float, int] | None = None,
    ) -> FloatArray:
        if self.spec.truth == "overfit":
            return _overfit_positions(self.spec, float(values["a"]), float(values["b"]))
        returns = self.market.returns
        if price_noise is not None:
            level, seed = price_noise
            closes = pd.Series(self.market.open_prices * np.exp(self.market.returns))
            seen = pd.Series(
                noisy_prices(closes, self.spec.spread_bps * _BPS * closes, level, seed)
            ).to_numpy(np.float64)
            returns = np.concatenate([[0.0], np.log(seen[1:] / seen[:-1])])
        t_stat = _trend_t_stat(returns, int(values["lookback"]))
        if feature_noise is not None:
            level, seed = feature_noise
            frame = noisy_features(pd.DataFrame({"t": t_stat}), level, seed)
            t_stat = frame["t"].to_numpy(np.float64)
        return _from_t_stat(t_stat, float(values["deadband"]))


def _grid(spec: SimulationSpec) -> list[dict[str, float]]:
    if spec.truth == "genuine":
        return [
            {"lookback": float(k), "deadband": d}
            for k in GENUINE_LOOKBACKS
            for d in GENUINE_DEADBANDS
        ]
    return [{"a": a, "b": b} for a in OVERFIT_A for b in OVERFIT_B]


def _name(values: Mapping[str, float]) -> str:
    return ",".join(f"{k}={v:g}" for k, v in values.items())


def _parameters(spec: SimulationSpec, values: Mapping[str, float]) -> tuple[Parameter, ...]:
    if spec.truth == "genuine":
        return (
            Parameter("lookback", values["lookback"], integer=True, minimum=2),
            Parameter("deadband", values["deadband"]),
        )
    return (Parameter("a", values["a"], integer=True, minimum=1), Parameter("b", values["b"]))


def _trades(market: _Market, positions: FloatArray, capital: float) -> pd.DataFrame:
    """Closed holding episodes of `positions` (open at the end and unknown sigma-hat dropped)."""
    rows = []
    n = len(positions)
    t = 0
    while t < n:
        if positions[t] == 0:
            t += 1
            continue
        end = t
        while end + 1 < n and positions[end + 1] == positions[t]:
            end += 1
        if end + 1 < n and np.isfinite(market.sigma[t]):
            sign = float(positions[t])
            gross = float(np.sum(sign * market.returns[t : end + 1]))
            costs = 2 * market.cost_per_turnover + market.financing * (end - t + 1)
            price = float(market.open_prices[t])
            trade_return = gross - costs
            rows.append(
                {
                    "entry_time": trading_day_bounds(market.days[t])[0] + pd.Timedelta(minutes=1),
                    "exit_time": trading_day_bounds(market.days[end])[1] - pd.Timedelta(minutes=1),
                    "direction": int(sign),
                    "entry_price": price,
                    "spread": market.spec.spread_bps * _BPS * price,
                    "sigma_daily": float(market.sigma[t]),
                    "trade_return": trade_return,
                    "pnl": trade_return * capital,
                }
            )
        t = end + 1
    return pd.DataFrame(rows, columns=list(TRADE_COLUMNS))


def _cost_stress(
    market: _Market, positions: FloatArray, gates: GatesConfig, periods_per_year: int
) -> CostStressResult:
    """ROB-002's scenarios on the daily synthetic asset (latency has no meaning on daily bars)."""
    stress = gates.r2_validated.stressed_costs
    scenarios = [
        CostScenario("baseline"),
        *(CostScenario(f"spread_x{m:g}", spread=m) for m in (1.25, 1.5, 2.0)),
        *(CostScenario(f"slippage_x{m:g}", slippage=m) for m in (2.0, 3.0)),
        CostScenario("financing_x1.5", financing=1.5),
        CostScenario(
            GATE_SCENARIO, spread=stress.spread_multiplier, slippage=stress.slippage_multiplier
        ),
    ]
    gross = float(np.sum(positions * market.returns))
    baseline_costs = gross - float(market.pnl(positions).sum())
    rows = []
    for scenario in scenarios:
        net = market.pnl(
            positions,
            spread=scenario.spread,
            slippage=scenario.slippage,
            commission=scenario.commission,
            financing=scenario.financing,
        )
        rows.append(
            {
                "scenario": scenario.name,
                "spread": scenario.spread,
                "slippage": scenario.slippage,
                "commission": scenario.commission,
                "financing": scenario.financing,
                "extra_latency_ms": 0,
                "net_sharpe": return_metrics(pd.Series(net), periods_per_year)["sharpe"],
                "net_pnl": float(net.sum()),
                "gross_pnl": gross,
                "costs": gross - float(net.sum()),
                "fills": int(np.count_nonzero(np.diff(positions, prepend=0.0))),
                "missed": 0,
            }
        )
    table = pd.DataFrame(rows).set_index("scenario")
    # net P&L is linear in the cost multiplier here, so the break-even is gross over costs
    label = "synthetic data, simulated costs"
    return CostStressResult(table, _break_even(gross, baseline_costs), label)


def _break_even(gross: float, costs: float) -> float:
    if costs <= 0:
        return math.inf if gross > 0 else 0.0
    return max(gross, 0.0) / costs


def simulated_subject(
    spec: SimulationSpec,
    *,
    capital: float,
    periods_per_year: int,
    trials: TrialSummary | None = None,
    slices: DeclaredSlices | None = None,
    sessions: SessionsConfig | None = None,
) -> StrategySubject:
    """The candidate of a simulated family, as a validation subject (module docstring).

    Args:
        trials: The family's trials as the registry counts them (a recorded run's, or
            `family_trials` of the subject's family). By default every configuration counts as
            an independent trial (raw), the most conservative count.
    """
    market = _Market(spec)
    strategy = _Strategy(market)
    grid = _grid(spec)
    family_positions = {_name(v): strategy.positions(v) for v in grid}
    family = pd.DataFrame(
        {name: market.pnl(p) for name, p in family_positions.items()}, index=market.days
    )
    sharpes = {name: sharpe_ratio(family[name].to_numpy()) for name in family.columns}
    chosen = grid[int(np.argmax([sharpes[_name(v)] for v in grid]))]
    name = _name(chosen)
    positions = family_positions[name]
    root = math.sqrt(periods_per_year)
    if trials is None:
        annual = np.array(list(sharpes.values())) * root
        trials = TrialSummary(len(grid), float(len(grid)), float(np.var(annual, ddof=1)), "raw")

    def evaluate(values: Mapping[str, float]) -> FloatArray:
        return market.pnl(strategy.positions({**chosen, **values}))

    noisy: dict[NoiseKind, NoisyEvaluate] = {}
    not_applicable: dict[NoiseKind, str] = {}
    if spec.truth == "genuine":
        noisy["price"] = lambda level, seed: market.pnl(
            strategy.positions(chosen, price_noise=(level, seed))
        )
        noisy["feature"] = lambda level, seed: market.pnl(
            strategy.positions(chosen, feature_noise=(level, seed))
        )
    else:
        reason = "the positions do not read market data"
        not_applicable = {"price": reason, "feature": reason}
    folds = np.minimum(np.arange(spec.days) * N_FOLDS // spec.days, N_FOLDS - 1)
    return StrategySubject(
        name=name,
        source=f"simulated {spec.truth} strategy, seed {spec.seed}",
        synthetic=True,
        cost_basis="synthetic data, simulated costs",
        capital=capital,
        periods_per_year=periods_per_year,
        returns=family[name].rename("return"),
        trades=_trades(market, positions, capital),
        family=family,
        folds=pd.Series([f"fold{k:02d}" for k in folds], index=market.days, name="fold"),
        trials=trials,
        parameters=_parameters(spec, chosen),
        evaluate=evaluate,
        cost_stress=lambda gates: _cost_stress(market, positions, gates, periods_per_year),
        delayed=lambda bars: market.pnl(_delayed(positions, bars)),
        noisy=noisy,
        noise_not_applicable=not_applicable,
        baselines=pd.DataFrame({"buy_and_hold": market.pnl(np.ones(spec.days))}, index=market.days),
        sigma_daily=pd.Series(market.sigma, index=market.days, name="sigma_daily"),
        slices=slices,
        sessions=sessions,
    )


def simulated_family_returns(spec: SimulationSpec) -> pd.DataFrame:
    """Daily net returns of every configuration of the spec's family (for recording trials)."""
    market = _Market(spec)
    strategy = _Strategy(market)
    return pd.DataFrame(
        {_name(v): market.pnl(strategy.positions(v)) for v in _grid(spec)}, index=market.days
    )
