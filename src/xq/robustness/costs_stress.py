"""Cost and latency stress (ROB-002).

A strategy is screened again (`run_vectorized`) with its costs made worse, one dimension at a
time, over the plan's grid:

- **Spread** x1.25, x1.5, x2: every quote is widened around its mid,
  ``bid' = mid - m (mid - bid)`` and ``ask' = mid + m (ask - mid)``, so fills pay m times the
  half-spread (the fill quote and the mid are unchanged);
- **Slippage** x2, x3: the cost model's fixed and sigma terms are multiplied (the session and event
  multipliers stay on top);
- **Latency** +250 ms, +1 s, +5 s of market time from decision to order;
- **Financing** x1.5: a charged rate is multiplied, a credited (negative) rate divided, so the
  stress always costs more;
- **The R2 scenario**: the gate's spread and slippage multipliers together (``stressed_costs`` in
  ``config/gates.yaml``: 1.5 and 2), whose net Sharpe ratio must exceed the gate's floor.

The **break-even cost multiplier** is the k at which every cost (spread, slippage, commission and
financing) multiplied by k leaves no net P&L. Net P&L is almost linear in k (the slippage is
charged on the widened side price), so k is found by the secant method on actual runs, starting
from the linear estimate ``gross P&L / total costs``. It is 0 when the strategy loses before costs
and infinite when it pays none. Latency is held at the model's own.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from xq.backtest.costs import CostModel
from xq.backtest.metrics import return_metrics
from xq.backtest.vectorized import BacktestResult, run_vectorized
from xq.core.config import GateCheck, GatesConfig
from xq.data.calendar import MarketClock

COST_COLUMNS = ("spread_cost", "slippage_cost", "commission", "financing")
GATE_SCENARIO = "r2_stressed_costs"
_SECANT_STEPS = 8


@dataclass(frozen=True)
class CostScenario:
    """Multipliers of each cost and the latency added, relative to the cost model."""

    name: str
    spread: float = 1.0
    slippage: float = 1.0
    commission: float = 1.0
    financing: float = 1.0
    extra_latency_ms: int = 0

    def __post_init__(self) -> None:
        multipliers = (self.spread, self.slippage, self.commission, self.financing)
        if min(multipliers) < 0 or self.extra_latency_ms < 0:
            raise ValueError(f"{self.name}: multipliers and added latency must not be negative")

    @classmethod
    def all_costs(cls, name: str, k: float) -> CostScenario:
        """Every cost multiplied by `k` (the break-even search)."""
        return cls(name, spread=k, slippage=k, commission=k, financing=k)


def plan_scenarios(gates: GatesConfig) -> tuple[CostScenario, ...]:
    """The plan's grid, one dimension at a time, and the R2 scenario (module docstring)."""
    stress = gates.r2_validated.stressed_costs
    return (
        CostScenario("baseline"),
        *(CostScenario(f"spread_x{m:g}", spread=m) for m in (1.25, 1.5, 2.0)),
        *(CostScenario(f"slippage_x{m:g}", slippage=m) for m in (2.0, 3.0)),
        *(CostScenario(f"latency_+{ms}ms", extra_latency_ms=ms) for ms in (250, 1000, 5000)),
        CostScenario("financing_x1.5", financing=1.5),
        CostScenario(
            GATE_SCENARIO, spread=stress.spread_multiplier, slippage=stress.slippage_multiplier
        ),
    )


def stressed_quotes(quotes: pd.DataFrame, spread: float) -> pd.DataFrame:
    """`quotes` with every spread multiplied by `spread` around its mid."""
    if spread == 1.0:
        return quotes
    mid = (quotes["bid"] + quotes["ask"]) / 2
    return quotes.assign(
        bid=mid - spread * (mid - quotes["bid"]), ask=mid + spread * (quotes["ask"] - mid)
    )


def stressed_costs(costs: CostModel, scenario: CostScenario) -> CostModel:
    """`costs` with the scenario's slippage, commission, financing and latency applied."""
    config = costs.config
    slippage = config.slippage.model_copy(
        update={
            "fixed_bps": config.slippage.fixed_bps * scenario.slippage,
            "sigma_multiple": config.slippage.sigma_multiple * scenario.slippage,
        }
    )
    commission = config.commission.model_copy(
        update={
            "per_lot_per_side_usd": config.commission.per_lot_per_side_usd * scenario.commission,
            "per_notional_per_side_bps": config.commission.per_notional_per_side_bps
            * scenario.commission,
        }
    )
    financing = config.financing.model_copy(
        update={
            "long_rate_annual_pct": _worse(
                config.financing.long_rate_annual_pct, scenario.financing
            ),
            "short_rate_annual_pct": _worse(
                config.financing.short_rate_annual_pct, scenario.financing
            ),
        }
    )
    stressed = config.model_copy(
        update={
            "latency_ms": config.latency_ms + scenario.extra_latency_ms,
            "slippage": slippage,
            "commission": commission,
            "financing": financing,
        }
    )
    return CostModel(stressed, costs.instrument, costs.sessions, costs.spread_stats)


@dataclass(frozen=True)
class CostStressResult:
    """Net results per scenario and the break-even cost multiplier (module docstring)."""

    #: One row per scenario: its multipliers, ``net_sharpe`` (annualized), ``net_pnl``,
    #: ``gross_pnl``, ``costs`` (their sum), ``fills`` and ``missed`` decisions.
    table: pd.DataFrame
    break_even_multiplier: float
    cost_basis: str

    def gate_check(self, gates: GatesConfig) -> GateCheck:
        """R2 ``stressed_costs``: the net Sharpe ratio of the gate's scenario."""
        criterion = gates.criterion("R2", "stressed_costs.net_sharpe_min")
        sharpe = self.table["net_sharpe"].to_numpy(np.float64)
        return criterion.check(float(sharpe[self.table.index.get_loc(GATE_SCENARIO)]))


def cost_stress(
    positions: pd.Series,
    quotes: pd.DataFrame,
    costs: CostModel,
    clock: MarketClock,
    *,
    capital: float,
    periods_per_year: int,
    gates: GatesConfig,
    sigma_1m_bps: pd.Series | None = None,
    scenarios: Sequence[CostScenario] | None = None,
) -> CostStressResult:
    """Screen `positions` under every scenario and find the break-even multiplier.

    Args:
        positions, quotes, costs, clock, capital, sigma_1m_bps: As for `run_vectorized`.
        periods_per_year: Annualization of the Sharpe ratio (``backtest.periods_per_year``).
        gates: The evidence policy (the R2 scenario's multipliers).
        scenarios: Scenarios to run (default `plan_scenarios`); the R2 scenario is always added.
    """
    chosen = list(scenarios if scenarios is not None else plan_scenarios(gates))
    if GATE_SCENARIO not in {s.name for s in chosen}:
        chosen.append(next(s for s in plan_scenarios(gates) if s.name == GATE_SCENARIO))
    if len({s.name for s in chosen}) != len(chosen):
        raise ValueError("scenario names must be distinct")

    def screen(scenario: CostScenario) -> BacktestResult:
        return run_vectorized(
            positions,
            stressed_quotes(quotes, scenario.spread),
            stressed_costs(costs, scenario),
            clock,
            capital=capital,
            sigma_1m_bps=sigma_1m_bps,
        )

    rows = []
    for scenario in chosen:
        result = screen(scenario)
        daily = result.daily
        rows.append(
            {
                "scenario": scenario.name,
                "spread": scenario.spread,
                "slippage": scenario.slippage,
                "commission": scenario.commission,
                "financing": scenario.financing,
                "extra_latency_ms": scenario.extra_latency_ms,
                "net_sharpe": return_metrics(daily["return"], periods_per_year)["sharpe"],
                "net_pnl": float(daily["net_pnl"].sum()),
                "gross_pnl": float(daily["gross_pnl"].sum()),
                "costs": _total_costs(result),
                "fills": len(result.fills),
                "missed": len(result.missed),
            }
        )
    table = pd.DataFrame(rows).set_index("scenario")
    return CostStressResult(table, _break_even(screen), costs.result_label)


def _worse(rate: float, multiplier: float) -> float:
    """A charged rate times the multiplier; a credited rate divided by it."""
    if rate >= 0:
        return rate * multiplier
    return rate / multiplier if multiplier > 0 else math.inf


def _total_costs(result: BacktestResult) -> float:
    return float(result.daily[list(COST_COLUMNS)].to_numpy(np.float64).sum())


def _break_even(screen: Callable[[CostScenario], BacktestResult]) -> float:
    """The all-cost multiplier with zero net P&L, by the secant method from the linear estimate."""

    def net(k: float) -> tuple[float, float]:
        result = screen(CostScenario.all_costs("break_even", k))
        return float(result.daily["net_pnl"].sum()), _total_costs(result)

    net_1, costs_1 = net(1.0)
    gross = net_1 + costs_1
    if costs_1 <= 0:
        return math.inf if gross > 0 else 0.0
    if gross <= 0:
        return 0.0
    k_prev, f_prev = 1.0, net_1
    k = gross / costs_1
    for _ in range(_SECANT_STEPS):
        f, _ = net(k)
        if abs(f) <= 1e-9 * costs_1 or f == f_prev:
            break
        k, k_prev, f_prev = k - f * (k - k_prev) / (f - f_prev), k, f
    return k
