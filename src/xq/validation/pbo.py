"""Probability of backtest overfitting by combinatorially symmetric cross-validation (VAL-003).

Bailey, Borwein, López de Prado and Zhu (2017), "The probability of backtest overfitting".

Given the per-period returns of N configurations tried on the same T periods (a T x N matrix, the
*configuration matrix*), split the periods into S contiguous blocks of equal size (``S`` even,
default 16; the last ``T mod S`` periods are dropped). For every choice of S/2 blocks as the
in-sample set — C(S, S/2) combinations, 12,870 for S = 16 — with the other half out of sample:

1. rank the configurations by their in-sample performance and pick the best, n*;
2. find n*'s relative rank among all N out of sample, ``w = rank / (N + 1)`` (rank 1 = worst);
3. record its logit ``lambda = ln(w / (1 - w))``.

The **probability of backtest overfitting** is the share of combinations whose logit is at most
zero: the in-sample winner ranks at or below the out-of-sample median. With no skill anywhere
(every configuration noise) the winner's out-of-sample rank is uniform and PBO is about 0.5. If
the in-sample winner keeps its edge out of sample, PBO is near 0. The report also gives the
out-of-sample performance of each combination's winner against its in-sample performance (the
degradation slope) and the probability that the winner loses out of sample.

Performance is the per-period Sharpe ratio (mean over standard deviation) by default, or the mean
return; a configuration with zero variance in a half has a Sharpe ratio of zero there. The blocks
are contiguous, so serial dependence stays inside them and no period is in both halves.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

FloatArray = npt.NDArray[np.float64]
Metric = Literal["sharpe", "mean"]
#: Combinations evaluated at once (bounds memory: chunk x N floats per statistic).
_CHUNK = 2048


@dataclass(frozen=True)
class PBOResult:
    """The CSCV outcome (module docstring)."""

    pbo: float
    logits: FloatArray
    is_performance: FloatArray
    oos_performance: FloatArray
    n_configurations: int
    n_blocks: int
    n_combinations: int
    metric: Metric

    @property
    def probability_of_loss(self) -> float:
        """Share of combinations whose in-sample winner performs below zero out of sample."""
        return float(np.mean(self.oos_performance < 0))

    @property
    def degradation_slope(self) -> float:
        """Least-squares slope of out-of-sample on in-sample performance of the winners.

        Descriptive: the two halves of a combination are complementary, so for a fixed
        configuration a better first half means a worse second half and the slope tends to be
        negative even for a genuine edge; PBO, not this slope, is the test.
        """
        x = self.is_performance - self.is_performance.mean()
        denominator = float(np.dot(x, x))
        if denominator == 0:
            return float("nan")
        return float(np.dot(x, self.oos_performance - self.oos_performance.mean()) / denominator)

    def summary(self) -> dict[str, float | int | str]:
        """The headline numbers for reports and the registry."""
        return {
            "pbo": self.pbo,
            "probability_of_loss": self.probability_of_loss,
            "degradation_slope": self.degradation_slope,
            "median_logit": float(np.median(self.logits)),
            "n_configurations": self.n_configurations,
            "n_blocks": self.n_blocks,
            "n_combinations": self.n_combinations,
            "metric": self.metric,
        }


def pbo_cscv(
    returns: pd.DataFrame | npt.ArrayLike, *, n_blocks: int = 16, metric: Metric = "sharpe"
) -> PBOResult:
    """PBO of a configuration matrix (rows periods, columns configurations; module docstring).

    Raises:
        ValueError: for an odd or too small number of blocks, fewer than two configurations,
            fewer periods than blocks, or missing values.
    """
    matrix = np.asarray(returns, dtype=np.float64)
    if matrix.ndim != 2:
        raise ValueError("the configuration matrix must be two-dimensional (periods x configs)")
    if n_blocks < 2 or n_blocks % 2:
        raise ValueError("the number of blocks must be even and at least 2")
    periods, n_configs = matrix.shape
    if n_configs < 2:
        raise ValueError("PBO needs at least two configurations")
    if periods < n_blocks:
        raise ValueError(f"{periods} periods cannot fill {n_blocks} blocks")
    if not np.isfinite(matrix).all():
        raise ValueError("the configuration matrix has missing or infinite values")
    size = periods // n_blocks
    blocks = matrix[: size * n_blocks].reshape(n_blocks, size, n_configs)
    sums = blocks.sum(axis=1)  # (S, N)
    squares = (blocks**2).sum(axis=1)
    combos = np.array(list(itertools.combinations(range(n_blocks), n_blocks // 2)), dtype=np.int64)
    everything = np.ones(n_blocks, dtype=bool)
    half = size * (n_blocks // 2)
    logits: list[FloatArray] = []
    is_best: list[FloatArray] = []
    oos_best: list[FloatArray] = []
    for start in range(0, len(combos), _CHUNK):
        chunk = combos[start : start + _CHUNK]
        in_mask = np.zeros((len(chunk), n_blocks), dtype=bool)
        np.put_along_axis(in_mask, chunk, True, axis=1)
        out_mask = everything & ~in_mask
        is_perf = _performance(in_mask @ sums, in_mask @ squares, half, metric)
        oos_perf = _performance(out_mask @ sums, out_mask @ squares, half, metric)
        winner = np.argmax(is_perf, axis=1)
        rows = np.arange(len(chunk))
        winner_oos = oos_perf[rows, winner]
        # rank 1 = worst; ties share the average rank
        below = (oos_perf < winner_oos[:, None]).sum(axis=1)
        ties = (oos_perf == winner_oos[:, None]).sum(axis=1)
        rank = below + (ties + 1) / 2
        w = rank / (n_configs + 1)
        logits.append(np.log(w / (1 - w)))
        is_best.append(is_perf[rows, winner])
        oos_best.append(winner_oos)
    all_logits = np.concatenate(logits)
    return PBOResult(
        pbo=float(np.mean(all_logits <= 0)),
        logits=all_logits,
        is_performance=np.concatenate(is_best),
        oos_performance=np.concatenate(oos_best),
        n_configurations=n_configs,
        n_blocks=n_blocks,
        n_combinations=len(combos),
        metric=metric,
    )


def n_combinations(n_blocks: int) -> int:
    """C(S, S/2): the combinations CSCV evaluates."""
    return math.comb(n_blocks, n_blocks // 2)


def _performance(sums: FloatArray, squares: FloatArray, n: int, metric: Metric) -> FloatArray:
    mean = sums / n
    if metric == "mean":
        return mean
    variance = np.maximum(squares / n - mean**2, 0.0) * n / (n - 1)
    std = np.sqrt(variance)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpe = np.where(std > 0, mean / std, 0.0)
    return np.asarray(sharpe, dtype=np.float64)
