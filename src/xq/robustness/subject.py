"""The strategy under validation: what the robustness and significance reports need (ROB-008).

A `StrategySubject` is built by an adapter for one kind of source (`xq.robustness.simulated` for
the known-truth simulated strategies; `xq.validation.subjects` for recorded runs). It carries:

- the strategy's evaluated results: its daily net returns, closed trades and walk-forward folds;
- the family of configurations it was selected from (for PBO, SPA and the deflated Sharpe
  ratio);
- functions that re-evaluate it under a changed parameter, cost, delay or noise.

The reports never see how a strategy is computed, only these results and functions, so every
kind of strategy is judged by the same code against the same gates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import GatesConfig, SessionsConfig, TrialClusteringConfig
from xq.core.time import trading_day_bounds
from xq.robustness.costs_stress import CostStressResult
from xq.robustness.noise import NoiseKind, NoisyEvaluate
from xq.robustness.perturb import Evaluate, Parameter
from xq.robustness.slicing import DeclaredSlices
from xq.tracking.trials import effective_trials
from xq.validation.sharpe import sharpe_ratio

#: Columns of `StrategySubject.trades`, one row per closed trade.
TRADE_COLUMNS = (
    "entry_time",
    "exit_time",
    "direction",
    "entry_price",
    "spread",
    "sigma_daily",
    "trade_return",
    "pnl",
)


@dataclass(frozen=True)
class TrialSummary:
    """The trials of the strategy's family, as the deflated Sharpe ratio uses them (EXP-004).

    `sharpe_variance` is the variance of the trials' **annualized** Sharpe ratios; `gated` names
    the count the gates use (``conventions.trial_count``).
    """

    n_raw: int
    n_effective: float
    sharpe_variance: float
    gated: Literal["effective", "raw"]

    @property
    def n_gated(self) -> float:
        """The trial count the gates read."""
        return self.n_effective if self.gated == "effective" else float(self.n_raw)


def family_trials(
    family: pd.DataFrame,
    *,
    clustering: TrialClusteringConfig,
    gated: Literal["effective", "raw"],
    periods_per_year: int,
) -> TrialSummary:
    """The trials of a family counted the way the registry counts them (EXP-004): every
    configuration is a raw trial, the effective count clusters their daily returns by
    correlation, and the variance is that of their annualized Sharpe ratios."""
    starts = pd.DatetimeIndex([trading_day_bounds(d)[0] for d in family.index])
    series = {
        str(c): pd.Series(family[c].to_numpy(np.float64), index=starts) for c in family.columns
    }
    root = np.sqrt(periods_per_year)
    sharpes = np.array([sharpe_ratio(s.to_numpy()) * root for s in series.values()])
    variance = float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else 0.0
    return TrialSummary(
        n_raw=len(series),
        n_effective=float(effective_trials(series, clustering)),
        sharpe_variance=variance,
        gated=gated,
    )


@dataclass(frozen=True)
class StrategySubject:
    """One strategy and everything its validation needs (module docstring)."""

    name: str
    #: Where the strategy comes from, for the report (a run id, or a simulation's spec).
    source: str
    #: Synthetic data or a simulated strategy: an engineering check, never evidence.
    synthetic: bool
    #: The label every net figure carries (for example "screening, placeholder costs").
    cost_basis: str
    capital: float
    periods_per_year: int
    #: Daily net returns (net P&L over capital) on every evaluated trading day, indexed by day.
    returns: pd.Series
    #: Closed trades (`TRADE_COLUMNS`); ``trade_return`` is net P&L over the notional at entry.
    trades: pd.DataFrame
    #: Daily net returns of every configuration of the family it was selected from (the
    #: strategy's own included), on the same days.
    family: pd.DataFrame
    #: Walk-forward test fold of each evaluated day.
    folds: pd.Series
    trials: TrialSummary
    #: Tunable parameters at their chosen values; empty when the strategy has none.
    parameters: tuple[Parameter, ...]
    #: Daily net returns at a parameter point (None without parameters).
    evaluate: Evaluate | None
    #: The ROB-002 cost and latency stress of the strategy.
    cost_stress: Callable[[GatesConfig], CostStressResult]
    #: Daily net returns with every order `k` decision bars late (ROB-007).
    delayed: Callable[[int], npt.ArrayLike]
    #: Daily net returns with noise injected, per kind of noise that applies (ROB-005).
    noisy: Mapping[NoiseKind, NoisyEvaluate]
    #: Why a kind of noise does not apply (for the report).
    noise_not_applicable: Mapping[NoiseKind, str] = field(default_factory=dict)
    #: Daily net returns of the baselines on the same days (R1: beat the best of them).
    baselines: pd.DataFrame | None = None
    #: Daily sigma-hat known at each evaluated day's start (volatility-tercile slices).
    sigma_daily: pd.Series | None = None
    #: The slices the tested hypothesis declared (ROB-006); None when there is no hypothesis.
    slices: DeclaredSlices | None = None
    sessions: SessionsConfig | None = None

    def __post_init__(self) -> None:
        if self.name not in self.family.columns:
            raise ValueError(f"the family must include the strategy {self.name!r}")
        if not self.returns.index.equals(self.family.index):
            raise ValueError("the family's days must be the strategy's days")
        if not self.folds.index.equals(self.returns.index):
            raise ValueError("every evaluated day needs its fold")
        if not np.isfinite(self.family.to_numpy(np.float64)).all():
            raise ValueError("the family's returns must not have missing values")
        missing = [c for c in TRADE_COLUMNS if c not in self.trades.columns]
        if missing:
            raise ValueError(f"trades lack columns {missing}")
        if self.baselines is not None and not self.baselines.index.equals(self.returns.index):
            raise ValueError("the baselines must cover the strategy's days")
        if self.parameters and self.evaluate is None:
            raise ValueError("a strategy with parameters needs an evaluate function")

    @property
    def daily_pnl(self) -> pd.Series:
        """Daily net P&L (USD)."""
        return self.returns * self.capital
