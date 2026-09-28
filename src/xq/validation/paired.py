"""The R1 test against the best baseline: a paired block bootstrap of the Sharpe difference.

R1 requires a research candidate to beat the best baseline. That means two things:

- its OOS net Sharpe ratio minus the best baseline's exceeds ``best_baseline_margin_sharpe``
  (0);
- the one-sided p-value of that difference is below ``best_baseline_p_max`` (0.10).

The **best baseline** is the baseline with the highest annualized net Sharpe ratio on the same
days. It is chosen after the fact, which only makes the test harder for the candidate.

The candidate's and the baseline's daily returns are resampled **together** (paired: the same
stationary-bootstrap indices for both), so their correlation is kept. The mean block follows the
gates' convention on the differential series (Politis-White, at least ``min_block_days``). The
p-value imposes the null of no difference by centring:

    p = (1 + #{(dSR*_b - dSR) >= dSR}) / (1 + B)

where dSR is the difference of annualized Sharpe ratios.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import GateBootstrapConfig, GateCheck, GatesConfig
from xq.validation.sharpe import bootstrap_distribution, gate_block_length, row_sharpe

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class BaselineTest:
    """The candidate against its best baseline (module docstring)."""

    baseline: str
    candidate_sharpe: float
    baseline_sharpe: float
    p_value: float
    mean_block: float
    n_boot: int

    @property
    def margin(self) -> float:
        """Annualized Sharpe ratio of the candidate minus the best baseline's."""
        return self.candidate_sharpe - self.baseline_sharpe

    def gate_checks(self, gates: GatesConfig) -> tuple[GateCheck, GateCheck]:
        """R1 ``best_baseline_p_max`` and ``best_baseline_margin_sharpe``."""
        return (
            gates.criterion("R1", "best_baseline_p_max").check(self.p_value),
            gates.criterion("R1", "best_baseline_margin_sharpe").check(self.margin),
        )


def beats_best_baseline(
    candidate: pd.Series,
    baselines: pd.DataFrame,
    *,
    bootstrap: GateBootstrapConfig,
    periods_per_year: int,
    seed: int,
    n_boot: int | None = None,
) -> BaselineTest:
    """The paired test of `candidate` against the best of `baselines` (module docstring).

    Args:
        candidate: Daily net returns of the candidate.
        baselines: Daily net returns of each baseline, on the candidate's days.
        bootstrap: The gates' bootstrap convention.
        periods_per_year: Annualization of the Sharpe ratios.
        seed: Seed of the resamples.
        n_boot: Resamples (default: the convention's).

    Raises:
        ValueError: without a baseline, on other days, or with missing values.
    """
    if baselines.shape[1] == 0:
        raise ValueError("the test needs at least one baseline")
    if not baselines.index.equals(candidate.index):
        raise ValueError("the baselines must cover the candidate's days")
    values = baselines.to_numpy(np.float64)
    c = candidate.to_numpy(np.float64)
    if not (np.isfinite(values).all() and np.isfinite(c).all()):
        raise ValueError("returns must not contain missing values")
    root = math.sqrt(periods_per_year)
    sharpes = row_sharpe(values.T) * root
    best = int(np.nanargmax(np.where(np.isfinite(sharpes), sharpes, -np.inf)))
    b = values[:, best]
    block = gate_block_length(c - b, bootstrap.block_length, bootstrap.min_block_days)
    draws = bootstrap.n_boot if n_boot is None else n_boot
    candidate_sharpe = float(row_sharpe(c[None, :])[0]) * root
    baseline_sharpe = float(sharpes[best])
    observed = candidate_sharpe - np.nan_to_num(baseline_sharpe)

    def difference(rows: FloatArray) -> FloatArray:
        index = rows.astype(np.int64)  # resampled day indices, shared by both series
        result: FloatArray = (
            np.nan_to_num(row_sharpe(c[index])) - np.nan_to_num(row_sharpe(b[index]))
        ) * root
        return result

    # resampling the day indices in chunks keeps memory bounded on long samples
    boot = bootstrap_distribution(
        np.arange(len(c), dtype=np.float64), difference, n_boot=draws, mean_block=block, seed=seed
    )
    p_value = float((1 + np.sum(boot - observed >= observed)) / (1 + draws))
    return BaselineTest(
        baseline=str(baselines.columns[best]),
        candidate_sharpe=candidate_sharpe,
        baseline_sharpe=baseline_sharpe,
        p_value=p_value,
        mean_block=block,
        n_boot=draws,
    )
