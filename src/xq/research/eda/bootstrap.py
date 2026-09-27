"""Stationary-bootstrap intervals for descriptive statistics of long series (EDA-002).

Minute returns over years are millions of rows, so resamples are drawn one at a time: each is a
sequence of blocks with geometric lengths of mean `mean_block` (the stationary bootstrap of
Politis and Romano 1994), built with vectorized index arithmetic in O(n) memory. The block length
keeps the dependence that matters for the moments of returns — volatility clustering — so it is the
Politis-White length of the *squared* returns, and never shorter than `min_block` bars (a number of
trading days of bars, ``config/eda.yaml``) unless that leaves fewer than ten blocks per resample.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping

import numpy as np
import numpy.typing as npt

from xq.core.seeds import make_rng
from xq.validation.sharpe import politis_white_block_length

FloatArray = npt.NDArray[np.float64]
#: Every resample holds at least about this many blocks (the block length is capped at n / 10).
MIN_BLOCKS = 10
Statistics = Callable[[FloatArray], Mapping[str, float]]


def stationary_resample(
    rng: np.random.Generator, n: int, mean_block: float
) -> npt.NDArray[np.int64]:
    """One stationary-bootstrap resample of indices ``0 .. n-1`` (blocks wrap around)."""
    if n < 1:
        raise ValueError("n must be positive")
    if mean_block < 1:
        raise ValueError("mean_block must be at least 1")
    p = 1.0 / mean_block
    lengths = rng.geometric(p, size=max(8, math.ceil(2 * n * p) + 8))
    while int(lengths.sum()) < n:
        lengths = np.concatenate([lengths, rng.geometric(p, size=len(lengths))])
    blocks = int(np.searchsorted(np.cumsum(lengths), n)) + 1
    lengths = lengths[:blocks]
    starts = rng.integers(0, n, size=blocks)
    first = np.cumsum(lengths) - lengths
    offsets = np.arange(int(lengths.sum()), dtype=np.int64) - np.repeat(first, lengths)
    index: npt.NDArray[np.int64] = (np.repeat(starts, lengths) + offsets) % n
    return index[:n]


def eda_block_length(values: npt.ArrayLike, min_block: int) -> float:
    """Politis-White block length of the squared demeaned values, at least `min_block`.

    Capped at a tenth of the series (at least 1), so every resample has about ten blocks or more:
    with fewer, the bootstrap cannot see the series' variability and its intervals are too narrow.
    """
    x = np.asarray(values, dtype=np.float64)
    cap = max(1.0, len(x) / MIN_BLOCKS)
    if len(x) < 4:
        return min(float(max(1, min_block)), cap)
    squared = (x - x.mean()) ** 2
    return min(max(politis_white_block_length(squared), float(min_block)), cap)


def bootstrap_intervals(
    values: npt.ArrayLike,
    statistics: Statistics,
    *,
    n_boot: int,
    mean_block: float,
    level: float,
    seed: int,
) -> dict[str, tuple[float, float]]:
    """Percentile intervals of every statistic `statistics` returns, from `n_boot` resamples.

    `statistics` maps a series to named values; the interval of each name is taken over the
    resamples where it is finite. Deterministic given `seed`.
    """
    x = np.asarray(values, dtype=np.float64)
    if len(x) < 3:
        return dict.fromkeys(statistics(x), (math.nan, math.nan))
    rng = make_rng(seed)
    draws: dict[str, list[float]] = {}
    for _ in range(n_boot):
        for name, value in statistics(x[stationary_resample(rng, len(x), mean_block)]).items():
            draws.setdefault(name, []).append(value)
    alpha = (1 - level) / 2
    intervals: dict[str, tuple[float, float]] = {}
    for name, values_ in draws.items():
        finite = np.asarray(values_, dtype=np.float64)
        finite = finite[np.isfinite(finite)]
        if len(finite) == 0:
            intervals[name] = (math.nan, math.nan)
            continue
        low, high = np.quantile(finite, [alpha, 1 - alpha])
        intervals[name] = (float(low), float(high))
    return intervals
