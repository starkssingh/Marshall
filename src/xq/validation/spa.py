"""Reality Check, SPA and Romano-Wolf across a tested strategy family (VAL-004).

The input is the matrix of per-period *differentials* ``d[t, k]`` of the K strategies of a family
against a benchmark (the strategies' net returns minus the benchmark's; with no benchmark, the net
returns themselves: cash earns zero). The family-wide null is that **no strategy beats the
benchmark**: ``max_k E[d_k] <= 0``. All tests share one set of stationary-bootstrap resamples of
the periods (Politis and Romano 1994), the same indices for every strategy, so the dependence
across strategies and over time is kept.

- **White's Reality Check** (2000): ``V = max_k sqrt(n) mean(d_k)``, compared with the bootstrap
  distribution of ``max_k sqrt(n) (mean*(d_k) - mean(d_k))``. It centres every strategy at the null
  boundary, so poor strategies make it conservative.
- **Hansen's SPA** (2005): the studentized statistic
  ``T = max(0, max_k sqrt(n) mean(d_k) / omega_k)`` (omega_k from the bootstrap), against
  bootstrap statistics recentred by ``g(mean(d_k))``: the *consistent* p-value keeps a clearly
  poor strategy's negative mean (``mean(d_k) < -omega_k sqrt(2 log log n / n)``) so it does not
  inflate the maximum; the *lower* and *upper* p-values are its liberal and conservative bounds
  (the upper one centres like the Reality Check). The consistent p-value is the one the R2 gate
  reads (``spa_p_max``).
- **Romano-Wolf step-down** (2005, adjusted p-values as in 2016): studentized statistics in
  descending order; each strategy's adjusted p-value is the bootstrap probability that the maximum
  of the centred statistics of it and every strategy below it reaches its statistic, made
  monotone. Strategies whose adjusted p-value is at most the level are the **survivors**: those
  with a positive edge after controlling the family-wise error.

p-values are ``(1 + #{bootstrap >= observed}) / (1 + B)``. The mean block length is Politis-White
on the family's average differential, at least ``min_block`` (the gates' convention: at least 5
trading days), unless given.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.seeds import make_rng
from xq.validation.sharpe import gate_block_length

FloatArray = npt.NDArray[np.float64]
_CHUNK = 256


@dataclass(frozen=True)
class FamilyTest:
    """Reality Check, SPA and Romano-Wolf of one strategy family (module docstring)."""

    names: tuple[str, ...]
    n: int
    mean_block: float
    n_boot: int
    means: FloatArray
    t_stats: FloatArray
    reality_check_stat: float
    reality_check_p: float
    spa_stat: float
    spa_p: float
    spa_p_lower: float
    spa_p_upper: float
    stepm_p: FloatArray

    def survivors(self, level: float) -> tuple[str, ...]:
        """Strategies whose Romano-Wolf adjusted p-value is at most `level`."""
        return tuple(name for name, p in zip(self.names, self.stepm_p, strict=True) if p <= level)

    def table(self) -> pd.DataFrame:
        """One row per strategy: mean differential, studentized statistic, adjusted p-value."""
        return pd.DataFrame(
            {"mean": self.means, "t_stat": self.t_stats, "romano_wolf_p": self.stepm_p},
            index=pd.Index(self.names, name="strategy"),
        )


def family_tests(
    differentials: pd.DataFrame | npt.ArrayLike,
    *,
    n_boot: int,
    seed: int,
    mean_block: float | None = None,
    min_block: int = 5,
) -> FamilyTest:
    """Reality Check, SPA and Romano-Wolf of a family (module docstring).

    Args:
        differentials: Periods x strategies; a DataFrame's columns name the strategies.
        n_boot: Bootstrap resamples.
        seed: Seed of the resamples.
        mean_block: Mean block length (default: Politis-White, at least `min_block`).
        min_block: Lower bound on the automatic block length.

    Raises:
        ValueError: for fewer than two periods, no strategy, or missing values.
    """
    names = (
        tuple(str(c) for c in differentials.columns)
        if isinstance(differentials, pd.DataFrame)
        else None
    )
    d = np.asarray(differentials, dtype=np.float64)
    if d.ndim == 1:
        d = d[:, None]
    n, k = d.shape
    if n < 2 or k < 1:
        raise ValueError("the family needs at least two periods and one strategy")
    if not np.isfinite(d).all():
        raise ValueError("the differentials have missing or infinite values")
    names = names or tuple(f"s{i}" for i in range(k))
    block = (
        float(mean_block)
        if mean_block is not None
        else gate_block_length(d.mean(axis=1), "politis_white", min_block)
    )
    means = d.mean(axis=0)
    boot = _bootstrap_means(d, n_boot=n_boot, mean_block=block, seed=seed)  # (B, K)
    root_n = math.sqrt(n)
    centred = root_n * (boot - means)  # sqrt(n) (mean* - mean)
    omega = centred.std(axis=0, ddof=1)
    omega = np.where(omega > 0, omega, np.inf)  # a constant strategy cannot lead the maximum
    t_stats = root_n * means / omega

    rc_stat = float(np.max(root_n * means))
    rc_p = _p_value(centred.max(axis=1), rc_stat)

    spa_stat = max(0.0, float(np.max(t_stats)))
    threshold = -math.sqrt(2 * math.log(math.log(n))) if n > math.e else -math.inf
    recentred = {
        "lower": np.maximum(means, 0.0),
        "consistent": np.where(t_stats >= threshold, means, 0.0),
        "upper": means,
    }
    spa_ps = {}
    for name, g in recentred.items():
        z = root_n * (boot - g) / omega
        spa_ps[name] = _p_value(np.maximum(z.max(axis=1), 0.0), spa_stat)

    stepm = _romano_wolf(t_stats, centred / omega)
    return FamilyTest(
        names=names,
        n=n,
        mean_block=block,
        n_boot=n_boot,
        means=means,
        t_stats=t_stats,
        reality_check_stat=rc_stat,
        reality_check_p=rc_p,
        spa_stat=spa_stat,
        spa_p=spa_ps["consistent"],
        spa_p_lower=spa_ps["lower"],
        spa_p_upper=spa_ps["upper"],
        stepm_p=stepm,
    )


def _bootstrap_means(d: FloatArray, *, n_boot: int, mean_block: float, seed: int) -> FloatArray:
    """Column means of `n_boot` stationary-bootstrap resamples of the rows of `d` (B x K)."""
    if mean_block < 1:
        raise ValueError("mean_block must be at least 1")
    n = len(d)
    rng = make_rng(seed)
    out = np.empty((n_boot, d.shape[1]))
    for start in range(0, n_boot, _CHUNK):
        size = min(_CHUNK, n_boot - start)
        begins = rng.integers(0, n, size=(size, n))
        restart = rng.random((size, n)) < 1 / mean_block
        index = np.empty((size, n), dtype=np.int64)
        index[:, 0] = begins[:, 0]
        for t in range(1, n):
            index[:, t] = np.where(restart[:, t], begins[:, t], (index[:, t - 1] + 1) % n)
        counts = np.zeros((size, n))
        np.add.at(counts, (np.repeat(np.arange(size), n), index.ravel()), 1.0)
        out[start : start + size] = counts @ d / n
    return out


def _p_value(distribution: FloatArray, observed: float) -> float:
    return float((1 + np.sum(distribution >= observed)) / (1 + len(distribution)))


def _romano_wolf(t_stats: FloatArray, centred_t: FloatArray) -> FloatArray:
    """Step-down adjusted p-values, in the strategies' order."""
    order = np.argsort(-t_stats, kind="stable")
    adjusted = np.empty(len(t_stats))
    running = 0.0
    for j, k in enumerate(order):
        remaining = order[j:]
        maxima = centred_t[:, remaining].max(axis=1)
        running = max(running, _p_value(maxima, float(t_stats[k])))
        adjusted[k] = running
    return adjusted
