"""The robustness report and score (ROB-008).

`robustness_report` runs every robustness measure on one strategy (a `StrategySubject`) and judges
the ones the evidence gates cover against ``config/gates.yaml``:

- ROB-001 parameter perturbation (the full +/-20 % grid, a one-at-a-time table, heat maps):
  R2 ``parameter_neighbourhood``;
- ROB-002 cost and latency stress and the break-even cost multiplier: ``stressed_costs``;
- ROB-003 block-bootstrap intervals of Sharpe, CAGR and drawdown, and trade-order permutation:
  reported;
- ROB-004 Monte Carlo equity with the risk engine: ``monte_carlo_drawdown``;
- ROB-005 noise injection and its degradation curves: reported (P2);
- ROB-006 pre-registered slices and the largest single year's share of P&L:
  ``max_single_year_pnl_share``;
- ROB-007 execution delay of 1-3 bars: ``execution_delay``;
- the share of walk-forward test folds with positive net P&L: ``positive_folds_share_min``;
- the maximum drawdown of the evaluated period, capital as the first peak (BT-003):
  ``oos_max_drawdown_max``.

**The robustness score** is the share of these seven robustness gates the strategy passes, among
those that could be evaluated. The **verdict** is:

- ``pass`` when every applicable one of the seven is evaluated and passed;
- ``fail`` when any fails;
- ``incomplete`` when none fails but one could not be evaluated, with the reason: a strategy with
  no tuned parameter and no numeric constant has no neighbourhood; one without closed trades has
  nothing to resample.

A gate is never passed by default. **Parameter-free strategies** (C-25, ADR 0057): a strategy
with no tuned parameter has its neighbourhood formed by every numeric constant of its
configuration (`xq.robustness.perturb.config_constants`). Only when the tested hypothesis declares
``parameters_fixed_a_priori: true`` with a ``source`` is the neighbourhood gate **not
applicable**: listed with the source, reported (when there are constants) but not gated, and left
out of the verdict and the score. Every other gate still applies.

Thresholds are read, never set, here. The statistical gates (DSR, PBO, SPA, the R1 tests) are the
significance report's (`xq.validation.report`).

Every measure is also returned as a `RobustnessResult`, the plan's ``robustness_results`` row:
the test, its parameters, its metrics, and whether it passed (None when it is reported, not
gated).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from xq.backtest.metrics import drawdown_metrics
from xq.core.config import GateCheck, GatesConfig, ValidationConfig
from xq.core.seeds import derive_seed
from xq.research.reports import markdown_table
from xq.risk.engine import RiskEngine
from xq.robustness.bootstrap import (
    ReturnsBootstrap,
    TradePermutation,
    bootstrap_returns,
    permute_trades,
)
from xq.robustness.costs_stress import CostStressResult
from xq.robustness.delay import DEFAULT_DELAYS, DelayCurve, delay_curve
from xq.robustness.montecarlo import MonteCarloResult, monte_carlo, trade_outcomes
from xq.robustness.noise import NoiseCurve, noise_curve
from xq.robustness.perturb import PerturbationResult, perturb
from xq.robustness.slicing import SliceReport, max_single_year_share, slice_pnl
from xq.robustness.subject import StrategySubject
from xq.validation.sharpe import gate_block_length

#: The R2 gates this report judges, in report order.
ROBUSTNESS_GATES = (
    "parameter_neighbourhood.profitable_share_min",
    "stressed_costs.net_sharpe_min",
    "monte_carlo_drawdown.below",
    "max_single_year_pnl_share",
    "execution_delay.net_sharpe_min",
    "positive_folds_share_min",
    "oos_max_drawdown_max",
)
SYNTHETIC_BANNER = (
    "**SYNTHETIC DATA OR A SIMULATED STRATEGY: an engineering check of the validation "
    "machinery, never evidence about XAUUSD.**"
)


@dataclass(frozen=True)
class RobustnessResult:
    """One measure of the report: the plan's ``robustness_results`` row."""

    test_id: str
    name: str
    params: dict[str, Any]
    metrics: dict[str, Any]
    #: Whether its gate passed; None when the measure is reported, not gated, or not evaluated.
    passed: bool | None


@dataclass(frozen=True)
class RobustnessReport:
    """Every robustness measure of one strategy and its gate score (module docstring)."""

    name: str
    source: str
    synthetic: bool
    cost_basis: str
    perturbation: PerturbationResult | None
    cost_stress: CostStressResult
    bootstrap: ReturnsBootstrap
    permutation: TradePermutation | None
    monte_carlo: MonteCarloResult | None
    noise: dict[str, NoiseCurve]
    noise_not_applicable: dict[str, str]
    delay: DelayCurve
    slices: SliceReport | None
    max_year_share: float
    folds: pd.DataFrame
    drawdown: dict[str, float]
    #: The gate's perturbation level (``parameter_neighbourhood.perturbation``).
    neighbourhood_level: float
    #: The robustness gates that could be evaluated, in `ROBUSTNESS_GATES` order.
    checks: tuple[GateCheck, ...]
    #: Gate key -> why it could not be evaluated.
    not_evaluated: dict[str, str] = field(default_factory=dict)
    #: Gate key -> why it does not apply (an owner's rule; module docstring).
    not_applicable: dict[str, str] = field(default_factory=dict)
    #: What the perturbed parameters are (``tuned`` or ``constants``) and the constants held.
    parameter_kind: str = "tuned"
    held_constants: tuple[str, ...] = ()

    @property
    def score(self) -> float:
        """Share of the evaluated robustness gates passed (NaN when none could be evaluated)."""
        if not self.checks:
            return math.nan
        return sum(c.passed for c in self.checks) / len(self.checks)

    @property
    def verdict(self) -> str:
        """``pass``, ``fail`` or ``incomplete`` (module docstring)."""
        if any(not c.passed for c in self.checks):
            return "fail"
        return "incomplete" if self.not_evaluated else "pass"

    def check(self, key: str) -> GateCheck | None:
        """The evaluated check of gate `key` (None when it was not evaluated)."""
        return next((c for c in self.checks if c.criterion.key == key), None)

    def results(self) -> list[RobustnessResult]:
        """One row per measure, for the ``robustness_results`` table."""
        rows: list[RobustnessResult] = []

        def passed(key: str) -> bool | None:
            check = self.check(key)
            return None if check is None else check.passed

        if self.perturbation is not None:
            hood = self.perturbation
            level = self.neighbourhood_level
            rows.append(
                RobustnessResult(
                    "ROB-001",
                    "parameter_perturbation",
                    {
                        "levels": list(hood.levels),
                        "parameters": {p.name: p.nominal for p in hood.parameters},
                        "parameter_kind": self.parameter_kind,
                        "held_constants": list(self.held_constants),
                        "design": hood.neighbourhood_design(level),
                        "gated": "parameter_neighbourhood.profitable_share_min"
                        not in self.not_applicable,
                    },
                    {
                        "nominal_sharpe": _num(hood.nominal_sharpe),
                        "profitable_share": _num(hood.profitable_share(level)),
                        # NaN when the nominal Sharpe ratio is not positive: JSON null
                        "median_to_nominal": _num(hood.median_to_nominal(level)),
                    },
                    passed("parameter_neighbourhood.profitable_share_min"),
                )
            )
        stress = self.cost_stress
        rows.append(
            RobustnessResult(
                "ROB-002",
                "cost_stress",
                {"scenarios": list(stress.table.index)},
                {
                    "net_sharpe": {str(k): _num(v) for k, v in stress.table["net_sharpe"].items()},
                    "break_even_multiplier": _num(stress.break_even_multiplier),
                },
                passed("stressed_costs.net_sharpe_min"),
            )
        )
        boot = self.bootstrap
        rows.append(
            RobustnessResult(
                "ROB-003",
                "block_bootstrap",
                {"n_boot": boot.n_boot, "mean_block": boot.mean_block, "level": boot.level},
                {
                    name: {"estimate": _num(i.estimate), "low": _num(i.low), "high": _num(i.high)}
                    for name, i in (
                        ("sharpe", boot.sharpe),
                        ("cagr", boot.cagr),
                        ("max_drawdown", boot.max_drawdown),
                    )
                },
                None,
            )
        )
        if self.permutation is not None:
            perm = self.permutation
            rows.append(
                RobustnessResult(
                    "ROB-003",
                    "trade_permutation",
                    {"n_trades": perm.n_trades, "n_perm": len(perm.max_drawdown)},
                    {
                        "max_drawdown_percentile": perm.percentile("max_drawdown"),
                        "under_water_percentile": perm.percentile("under_water"),
                    },
                    None,
                )
            )
        if self.monte_carlo is not None:
            mc = self.monte_carlo
            rows.append(
                RobustnessResult(
                    "ROB-004",
                    "monte_carlo",
                    {"n_paths": mc.n_paths, "engine": mc.engine_label, "ruin_level": mc.ruin_level},
                    {str(k): _num(v) for k, v in mc.summary()["value"].items()},
                    passed("monte_carlo_drawdown.below"),
                )
            )
        for kind, curve in self.noise.items():
            rows.append(
                RobustnessResult(
                    "ROB-005",
                    f"{kind}_noise",
                    {"levels": list(curve.levels), "draws": int(curve.sharpe.shape[1])},
                    {
                        "median_sharpe": [_num(x) for x in curve.median()],
                        "breakdown_level": _num(curve.breakdown_level),
                    },
                    None,
                )
            )
        rows.append(
            RobustnessResult(
                "ROB-006",
                "slices",
                {"declared": [] if self.slices is None else list(self.slices.declared.names)},
                {"max_single_year_pnl_share": _num(self.max_year_share)},
                passed("max_single_year_pnl_share"),
            )
        )
        rows.append(
            RobustnessResult(
                "ROB-007",
                "execution_delay",
                {"delays": list(self.delay.delays)},
                {
                    "sharpe": [_num(x) for x in self.delay.sharpe],
                    "flips": self.delay.flips,
                },
                passed("execution_delay.net_sharpe_min"),
            )
        )
        rows.append(
            RobustnessResult(
                "WF",
                "positive_folds",
                {"folds": len(self.folds)},
                {"positive_share": _num(float((self.folds["net_pnl"] > 0).mean()))},
                passed("positive_folds_share_min"),
            )
        )
        rows.append(
            RobustnessResult(
                "BT-003",
                "oos_max_drawdown",
                {},
                {k: _num(v) for k, v in self.drawdown.items()},
                passed("oos_max_drawdown_max"),
            )
        )
        return rows

    def summary_lines(self) -> list[str]:
        """The score, the verdict and every gate line."""
        lines = [
            f"robustness score {self.score:.2f} ({sum(c.passed for c in self.checks)} of "
            f"{len(self.checks)} evaluated R2 robustness gates passed); verdict: {self.verdict}"
        ]
        lines.extend(c.describe() for c in self.checks)
        lines.extend(f"R2 {key}: not evaluated: {why}" for key, why in self.not_evaluated.items())
        lines.extend(f"R2 {key}: not applicable: {why}" for key, why in self.not_applicable.items())
        return lines

    def markdown(self) -> str:
        """The report as Markdown (the ROB-008 section of the validation report)."""
        out = ["## Robustness (ROB-001 ... ROB-008)", ""]
        if self.synthetic:
            out += [SYNTHETIC_BANNER, ""]
        out += [f"Net figures: {self.cost_basis}.", ""]
        out += ["### Score against config/gates.yaml", ""]
        out += [f"- {line}" for line in self.summary_lines()] + [""]
        if self.perturbation is not None:
            hood = self.perturbation
            out += ["### ROB-001 Parameter perturbation", ""]
            if self.parameter_kind == "constants":
                held = ", ".join(self.held_constants) or "none"
                out += [
                    "The strategy has no tuned parameter: every numeric constant of its "
                    f"configuration is perturbed (held at zero: {held}).",
                    "",
                ]
            why = self.not_applicable.get("parameter_neighbourhood.profitable_share_min")
            if why is not None:
                out += [f"Reported, not gated: {why}.", ""]
            design = hood.neighbourhood_design(self.neighbourhood_level)
            out += [f"Gate neighbourhood (±{self.neighbourhood_level:.0%}): {design}.", ""]
            out += [markdown_table(hood.summary().reset_index()), ""]
            out += ["One-at-a-time sensitivity (reported, not gated):", ""]
            out += [markdown_table(hood.sensitivity().reset_index()), ""]
        out += ["### ROB-002 Cost and latency stress", ""]
        out += [markdown_table(self.cost_stress.table.reset_index()), ""]
        out += [
            f"Break-even cost multiplier: {self.cost_stress.break_even_multiplier:.3g}.",
            "",
        ]
        out += ["### ROB-003 Block bootstrap and trade order", ""]
        out += [markdown_table(self.bootstrap.table().reset_index()), ""]
        if self.permutation is not None:
            out += [markdown_table(self.permutation.summary().reset_index()), ""]
        out += ["### ROB-004 Monte Carlo with the risk engine", ""]
        if self.monte_carlo is not None:
            out += [f"{self.monte_carlo.engine_label}; {self.monte_carlo.n_paths} paths.", ""]
            out += [markdown_table(self.monte_carlo.summary().reset_index()), ""]
        else:
            out += ["Not evaluated: no closed trades.", ""]
        out += ["### ROB-005 Noise injection (reported, not gated)", ""]
        for curve in self.noise.values():
            out += [markdown_table(curve.table().reset_index()), ""]
        for kind, why in self.noise_not_applicable.items():
            out += [f"{kind} noise: not applicable ({why}).", ""]
        out += ["### ROB-006 Pre-registered slices", ""]
        out += [f"Largest single-year share of net P&L: {self.max_year_share:.3g}.", ""]
        if self.slices is not None:
            for name, table in self.slices.tables.items():
                out += [f"**{name}** ({self.slices.label(name)})", ""]
                out += [markdown_table(table.reset_index()), ""]
        else:
            out += ["No hypothesis slices: the source registered none.", ""]
        out += ["### ROB-007 Execution delay", ""]
        out += [markdown_table(self.delay.table().reset_index()), ""]
        out += ["### Walk-forward folds", ""]
        out += [markdown_table(self.folds.reset_index()), ""]
        return "\n".join(out)


def robustness_report(
    subject: StrategySubject,
    *,
    gates: GatesConfig,
    settings: ValidationConfig,
    risk_engine: RiskEngine,
    seed: int,
) -> RobustnessReport:
    """Run ROB-001 ... ROB-007 on `subject` and score them against `gates` (module docstring).

    Args:
        subject: The strategy and its re-evaluation functions.
        gates: The evidence policy (thresholds and the bootstrap convention).
        settings: ``config/validation.yaml``.
        risk_engine: The engine the Monte Carlo replays trades through (the configured profile).
        seed: Base seed; every measure's seed is derived from it.
    """
    r2 = gates.r2_validated
    periods = subject.periods_per_year
    checks: dict[str, GateCheck] = {}
    not_evaluated: dict[str, str] = {}
    not_applicable: dict[str, str] = {}

    hood_key = "parameter_neighbourhood.profitable_share_min"
    perturbation = None
    if subject.parameters and subject.evaluate is not None:
        perturbation = perturb(
            subject.evaluate,
            subject.parameters,
            periods_per_year=periods,
            max_points=settings.perturbation.max_joint_points,
            seed=derive_seed(seed, "perturbation"),
            levels=settings.perturbation.levels,
            heatmaps=len(subject.parameters) >= 2,
        )
    if subject.parameters_fixed_a_priori is not None:
        not_applicable[hood_key] = (
            f"parameters fixed a priori (source: {subject.parameters_fixed_a_priori})"
        )
    elif perturbation is not None:
        checks[hood_key] = perturbation.gate_check(gates)
    else:
        not_evaluated[hood_key] = subject.neighbourhood_unavailable or (
            "the strategy has no tunable parameters and no numeric constant to perturb"
        )

    stress = subject.cost_stress(gates)
    checks["stressed_costs.net_sharpe_min"] = stress.gate_check(gates)

    returns = subject.returns.to_numpy(np.float64)
    convention = gates.conventions.bootstrap
    block = gate_block_length(returns, convention.block_length, convention.min_block_days)
    boot = bootstrap_returns(
        returns,
        periods_per_year=periods,
        n_boot=convention.n_boot,
        seed=derive_seed(seed, "returns_bootstrap"),
        mean_block=block,
    )
    trades = subject.trades
    permutation = None
    monte = None
    if len(trades):
        permutation = permute_trades(
            trades["pnl"].to_numpy(np.float64),
            capital=subject.capital,
            n_perm=convention.n_boot,
            seed=derive_seed(seed, "trade_permutation"),
        )
        mc = settings.monte_carlo
        monte = monte_carlo(
            trade_outcomes(trades, stop_sigmas=mc.stop_sigmas),
            risk_engine,
            capital=subject.capital,
            n_paths=mc.n_paths,
            seed=derive_seed(seed, "monte_carlo"),
            ruin_level=mc.ruin_level,
            min_block_trades=mc.min_block_trades,
        )
        checks["monte_carlo_drawdown.below"] = monte.gate_check(gates)
    else:
        not_evaluated["monte_carlo_drawdown.below"] = "no closed trades to resample"

    noise_settings = settings.noise
    curves: dict[str, NoiseCurve] = {}
    for kind, evaluate in subject.noisy.items():
        levels = noise_settings.price_levels if kind == "price" else noise_settings.feature_levels
        curves[kind] = noise_curve(
            evaluate,
            kind=kind,
            levels=levels,
            n_seeds=noise_settings.n_seeds,
            seed=derive_seed(seed, "noise"),
            periods_per_year=periods,
        )

    daily_pnl = subject.daily_pnl
    slices = None
    if subject.slices is not None and subject.sessions is not None:
        slices = slice_pnl(
            subject.slices,
            daily_pnl,
            trades.assign(open=False),
            capital=subject.capital,
            periods_per_year=periods,
            sessions=subject.sessions,
            sigma=subject.sigma_daily,
        )
    year_share = max_single_year_share(daily_pnl)
    checks["max_single_year_pnl_share"] = gates.criterion("R2", "max_single_year_pnl_share").check(
        year_share
    )

    delays = sorted({*DEFAULT_DELAYS, r2.execution_delay.bars})
    delay = delay_curve(subject.delayed, periods_per_year=periods, delays=delays)
    checks["execution_delay.net_sharpe_min"] = delay.gate_check(gates)

    folds = (
        pd.DataFrame({"fold": subject.folds, "net_pnl": daily_pnl})
        .groupby("fold", sort=True)["net_pnl"]
        .agg(["size", "sum"])
        .rename(columns={"size": "days", "sum": "net_pnl"})
    )
    checks["positive_folds_share_min"] = gates.criterion("R2", "positive_folds_share_min").check(
        float((folds["net_pnl"] > 0).mean())
    )

    equity = subject.capital * (1 + np.cumsum(returns))
    drawdown = drawdown_metrics(pd.Series(equity), subject.capital)
    checks["oos_max_drawdown_max"] = gates.criterion("R2", "oos_max_drawdown_max").check(
        drawdown["max_drawdown"]
    )

    return RobustnessReport(
        name=subject.name,
        source=subject.source,
        synthetic=subject.synthetic,
        cost_basis=subject.cost_basis,
        perturbation=perturbation,
        cost_stress=stress,
        bootstrap=boot,
        permutation=permutation,
        monte_carlo=monte,
        noise=curves,
        noise_not_applicable={str(k): v for k, v in subject.noise_not_applicable.items()},
        delay=delay,
        slices=slices,
        max_year_share=year_share,
        folds=folds,
        drawdown=drawdown,
        neighbourhood_level=r2.parameter_neighbourhood.perturbation,
        checks=tuple(checks[key] for key in ROBUSTNESS_GATES if key in checks),
        not_evaluated=not_evaluated,
        not_applicable=not_applicable,
        parameter_kind=subject.parameter_kind,
        held_constants=subject.held_constants,
    )


def _num(value: Any) -> float | None:
    """A JSON-safe number: NaN and infinities become None."""
    number = float(value)
    return number if math.isfinite(number) else None
