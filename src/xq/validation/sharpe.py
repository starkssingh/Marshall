"""Sharpe ratio inference (VAL-001).

Everything is in *per-period* units (for example daily): a Sharpe ratio ``sr`` is mean / standard
deviation of the period returns, and ``n`` is the number of periods. Annualize at the end with
`annualization_factor` (``sqrt(P)`` for serially uncorrelated returns, Lo's eta(q) otherwise).

- `se_iid`: ``sqrt((1 + sr^2 / 2) / n)`` for i.i.d. normal returns (Lo 2002).
- `se_non_normal`: ``sqrt((1 + sr^2 / 2 - skew * sr + (kurt - 3) / 4 * sr^2) / n)`` for i.i.d.
  returns with skewness and (non-excess) kurtosis (Mertens 2002; Opdyke 2007).
- `se_hac`: the delta-method GMM standard error with a Newey-West long-run covariance of the
  moments ``(r - mu, (r - mu)^2 - sigma^2)`` (Lo 2002, section 3), for serially correlated or
  heteroskedastic returns.
- `annualization_factor`: Lo's ``eta(q) = q / sqrt(q + 2 sum_{k<q} (q - k) rho_k)`` with sample
  autocorrelations up to `max_lag` (zero beyond); ``sqrt(q)`` when they are zero.
- `bootstrap_ci`: a percentile interval from the stationary bootstrap (Politis and Romano 1994),
  which keeps serial dependence within blocks of geometric length.
- `min_track_record_length`: the number of periods needed for the observed Sharpe ratio to exceed
  a benchmark with confidence ``1 - alpha`` (Bailey and Lopez de Prado 2012):
  ``1 + (1 - skew * sr + (kurt - 1) / 4 * sr^2) * (z / (sr - sr_benchmark))^2``.

Gate conventions (VAL-007, ``config/gates.yaml``, ADR 0032):

- `politis_white_block_length`: the automatic mean block length of the stationary bootstrap
  (Politis and White 2004, with the correction of Patton, Politis and White 2009):
  ``b = (2 G^2 / D)^(1/3) n^(1/3)`` with ``G = sum_{|k|<=M} lambda(k/M) |k| R(k)``,
  ``D = 2 (sum_{|k|<=M} lambda(k/M) R(k))^2``, the flat-top window ``lambda`` (1 up to 1/2, then
  falling linearly to 0 at 1) and ``M = 2 m``, where m is the first lag from which ``K_n =
  max(5, sqrt(log10 n))`` consecutive autocorrelations are all below ``2 sqrt(log10(n) / n)``
  (``M = ceil(sqrt(n)) + K_n`` if there is none); b is capped at ``ceil(min(3 sqrt(n), n / 3))``.
  `gate_block_length` floors it at the gates' ``min_block_days``.
- `bootstrap_sharpe`: the percentile interval and the one-sided p-value of ``SR > 0`` from one set
  of stationary-bootstrap resamples. The p-value imposes the null by centring the bootstrap
  distribution on the estimate: ``p = (1 + #{SR*_b - SR >= SR}) / (1 + B)``.
- `bootstrap_distribution`: any statistic of the resampled series (rows of resampled returns),
  for intervals on other metrics.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
from scipy.stats import kurtosis, norm, skew

from xq.core.seeds import make_rng

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class SharpeEstimate:
    """A per-period Sharpe ratio with its moments and standard errors."""

    sharpe: float
    n: int
    skew: float
    kurtosis: float
    se_iid: float
    se_non_normal: float
    se_hac: float


def sharpe_ratio(returns: npt.ArrayLike) -> float:
    """Per-period Sharpe ratio: mean over standard deviation (ddof = 1); NaN without variance."""
    r = _clean(returns)
    if len(r) < 2:
        return math.nan
    std = float(np.std(r, ddof=1))
    return float(np.mean(r)) / std if std > 0 else math.nan


def moments(returns: npt.ArrayLike) -> tuple[float, float]:
    """Sample skewness and non-excess kurtosis (3 for a normal distribution)."""
    r = _clean(returns)
    if len(r) < 3 or np.std(r) == 0:
        return math.nan, math.nan
    return float(skew(r)), float(kurtosis(r, fisher=False))


def se_iid(sr: float, n: int) -> float:
    """Standard error of a Sharpe ratio for i.i.d. normal returns."""
    return math.sqrt((1 + sr**2 / 2) / n)


def se_non_normal(sr: float, n: int, skewness: float, kurt: float) -> float:
    """Standard error for i.i.d. returns with skewness and non-excess kurtosis (Mertens)."""
    variance = (1 + sr**2 / 2 - skewness * sr + (kurt - 3) / 4 * sr**2) / n
    return math.sqrt(variance) if variance > 0 else math.nan


def se_hac(returns: npt.ArrayLike, max_lag: int | None = None) -> float:
    """GMM (delta method) standard error with a Newey-West covariance of the moments.

    `max_lag` defaults to ``floor(4 * (n / 100)^(2/9))`` (Newey and West 1994).
    """
    r = _clean(returns)
    n = len(r)
    if n < 3:
        return math.nan
    mu = float(np.mean(r))
    centred = r - mu
    var = float(np.mean(centred**2))
    if var == 0:
        return math.nan
    moments_t = np.column_stack([centred, centred**2 - var])
    lags = _default_lags(n) if max_lag is None else max_lag
    omega = _newey_west(moments_t, lags)
    sigma = math.sqrt(var)
    gradient = np.array([1 / sigma, -mu / (2 * sigma**3)])
    variance = float(gradient @ omega @ gradient) / n
    return math.sqrt(variance) if variance > 0 else math.nan


def annualization_factor(returns: npt.ArrayLike, q: int, max_lag: int | None = None) -> float:
    """Lo's eta(q): multiply a per-period Sharpe ratio by it to get the q-period one.

    Autocorrelations beyond `max_lag` (default ``min(q - 1, 10)``) are taken as zero.
    """
    if q < 1:
        raise ValueError("q must be positive")
    r = _clean(returns)
    lags = min(q - 1, 10) if max_lag is None else min(max_lag, q - 1)
    rho = autocorrelations(r, lags)
    k = np.arange(1, lags + 1)
    denominator = q + 2 * float(np.sum((q - k) * rho))
    return q / math.sqrt(denominator) if denominator > 0 else math.nan


def eta_for_autocorrelations(rho: npt.ArrayLike, q: int) -> float:
    """Lo's eta(q) for given autocorrelations ``rho[0] = rho_1, rho[1] = rho_2, ...``."""
    values = np.asarray(rho, dtype=np.float64)[: q - 1]
    k = np.arange(1, len(values) + 1)
    return q / math.sqrt(q + 2 * float(np.sum((q - k) * values)))


def autocorrelations(returns: npt.ArrayLike, max_lag: int) -> FloatArray:
    """Sample autocorrelations at lags 1..max_lag."""
    r = _clean(returns)
    centred = r - np.mean(r)
    denominator = float(np.sum(centred**2))
    if denominator == 0:
        return np.zeros(max_lag)
    return np.array(
        [float(np.sum(centred[k:] * centred[:-k])) / denominator for k in range(1, max_lag + 1)]
    )


def estimate(returns: npt.ArrayLike, max_lag: int | None = None) -> SharpeEstimate:
    """The per-period Sharpe ratio of `returns` with all three standard errors."""
    r = _clean(returns)
    sr = sharpe_ratio(r)
    skewness, kurt = moments(r)
    return SharpeEstimate(
        sharpe=sr,
        n=len(r),
        skew=skewness,
        kurtosis=kurt,
        se_iid=se_iid(sr, len(r)) if len(r) else math.nan,
        se_non_normal=se_non_normal(sr, len(r), skewness, kurt) if len(r) else math.nan,
        se_hac=se_hac(r, max_lag),
    )


def bootstrap_ci(
    returns: npt.ArrayLike,
    *,
    level: float = 0.95,
    n_boot: int = 2000,
    mean_block: float = 5.0,
    seed: int,
) -> tuple[float, float]:
    """Percentile interval of the per-period Sharpe ratio from the stationary bootstrap."""
    r = _clean(returns)
    if len(r) < 3:
        return math.nan, math.nan
    samples = stationary_bootstrap(len(r), n_boot=n_boot, mean_block=mean_block, seed=seed)
    draws = r[samples]
    std = np.std(draws, axis=1, ddof=1)
    ratios = np.where(std > 0, np.mean(draws, axis=1) / np.where(std > 0, std, 1.0), np.nan)
    alpha = (1 - level) / 2
    low, high = np.nanquantile(ratios, [alpha, 1 - alpha])
    return float(low), float(high)


def stationary_bootstrap(
    n: int, *, n_boot: int, mean_block: float, seed: int
) -> npt.NDArray[np.int64]:
    """``n_boot`` rows of `n` resampled indices; blocks have geometric lengths of mean `mean_block`.

    Each index follows the previous one (wrapping around) except that, with probability
    ``1 / mean_block``, a new block starts at a uniformly random position.
    """
    if mean_block < 1:
        raise ValueError("mean_block must be at least 1")
    return _stationary_indices(make_rng(seed), n, n_boot, mean_block)


def _stationary_indices(
    rng: np.random.Generator, n: int, n_boot: int, mean_block: float
) -> npt.NDArray[np.int64]:
    starts = rng.integers(0, n, size=(n_boot, n))
    restart = rng.random((n_boot, n)) < 1 / mean_block
    index = np.empty((n_boot, n), dtype=np.int64)
    index[:, 0] = starts[:, 0]
    for t in range(1, n):
        index[:, t] = np.where(restart[:, t], starts[:, t], (index[:, t - 1] + 1) % n)
    return index


def bootstrap_distribution(
    returns: npt.ArrayLike,
    statistic: Callable[[FloatArray], FloatArray],
    *,
    n_boot: int,
    mean_block: float,
    seed: int,
    chunk: int = 500,
) -> FloatArray:
    """`statistic` of `n_boot` stationary-bootstrap resamples of `returns`.

    `statistic` maps a 2-D array (one resampled series per row) to one value per row. Resamples
    are drawn in chunks of `chunk` rows from one generator seeded with `seed`, so the result
    depends only on the arguments, and memory stays bounded for long series.
    """
    if mean_block < 1:
        raise ValueError("mean_block must be at least 1")
    r = _clean(returns)
    rng = make_rng(seed)
    parts = []
    for size in [chunk] * (n_boot // chunk) + ([n_boot % chunk] if n_boot % chunk else []):
        parts.append(
            np.asarray(statistic(r[_stationary_indices(rng, len(r), size, mean_block)]), float)
        )
    return np.concatenate(parts) if parts else np.array([], dtype=np.float64)


def row_sharpe(draws: FloatArray) -> FloatArray:
    """Per-period Sharpe ratio of each row (NaN for a row without variance)."""
    std = np.std(draws, axis=1, ddof=1)
    safe = np.where(std > 0, std, 1.0)
    result: FloatArray = np.where(std > 0, np.mean(draws, axis=1) / safe, np.nan)
    return result


@dataclass(frozen=True)
class SharpeBootstrap:
    """Stationary-bootstrap inference on a per-period Sharpe ratio."""

    sharpe: float
    ci_low: float
    ci_high: float
    #: One-sided p-value of ``SR > 0`` (the null ``SR <= 0`` imposed by centring).
    p_value: float
    level: float
    mean_block: float
    n_boot: int


def bootstrap_sharpe(
    returns: npt.ArrayLike,
    *,
    n_boot: int,
    mean_block: float,
    seed: int,
    level: float = 0.95,
) -> SharpeBootstrap:
    """Percentile interval and one-sided p-value of the Sharpe ratio (see the module docstring)."""
    r = _clean(returns)
    sr = sharpe_ratio(r)
    if len(r) < 3 or math.isnan(sr):
        return SharpeBootstrap(sr, math.nan, math.nan, math.nan, level, mean_block, n_boot)
    draws = bootstrap_distribution(r, row_sharpe, n_boot=n_boot, mean_block=mean_block, seed=seed)
    valid = draws[~np.isnan(draws)]
    alpha = (1 - level) / 2
    low, high = np.quantile(valid, [alpha, 1 - alpha])
    p_value = (1 + int(np.sum(valid - sr >= sr))) / (1 + len(valid))
    return SharpeBootstrap(sr, float(low), float(high), p_value, level, mean_block, n_boot)


def politis_white_block_length(returns: npt.ArrayLike) -> float:
    """Automatic mean block length of the stationary bootstrap (see the module docstring).

    1 for a series too short or without variance.
    """
    r = _clean(returns)
    n = len(r)
    if n < 4 or np.all(r == r[0]):
        return 1.0
    eps = r - np.mean(r)
    k_n = max(5, math.ceil(math.sqrt(math.log10(n))))
    m_max = math.ceil(math.sqrt(n)) + k_n
    b_max = math.ceil(min(3 * math.sqrt(n), n / 3))
    lags = min(m_max, n - 1)
    acv = np.array([float(eps[k:] @ eps[: n - k]) / n for k in range(lags + 1)])
    if acv[0] <= 0:
        return 1.0
    critical = 2 * math.sqrt(math.log10(n) / n)
    insignificant = np.abs(acv / acv[0]) < critical
    m_hat = next(
        (m for m in range(1, lags - k_n + 2) if insignificant[m : m + k_n].all()),
        None,
    )
    bandwidth = min(m_max if m_hat is None else 2 * m_hat, lags)
    k = np.arange(1, bandwidth + 1)
    window = np.where(k / bandwidth <= 0.5, 1.0, 2 * (1 - k / bandwidth))
    g = 2 * float(np.sum(window * k * acv[1 : bandwidth + 1]))
    long_run = acv[0] + 2 * float(np.sum(window * acv[1 : bandwidth + 1]))
    if g == 0 or long_run == 0:
        return 1.0
    block = (2 * g**2 / (2 * long_run**2)) ** (1 / 3) * n ** (1 / 3)
    return float(min(block, b_max))


def gate_block_length(
    returns: npt.ArrayLike, rule: Literal["politis_white"] | int, min_block: int
) -> float:
    """Mean block length by the gate conventions: Politis-White or fixed, at least `min_block`."""
    base = politis_white_block_length(returns) if rule == "politis_white" else float(rule)
    return max(base, float(min_block))


def min_track_record_length(
    sr: float, skewness: float, kurt: float, *, sr_benchmark: float = 0.0, alpha: float = 0.05
) -> float:
    """Periods needed to reject ``SR <= sr_benchmark`` at level `alpha` (inf if unattainable)."""
    if sr <= sr_benchmark:
        return math.inf
    z = float(norm.ppf(1 - alpha))
    spread = 1 - skewness * sr + (kurt - 1) / 4 * sr**2
    return 1 + spread * (z / (sr - sr_benchmark)) ** 2


def _newey_west(values: FloatArray, lags: int) -> FloatArray:
    """Bartlett-weighted long-run covariance of the rows of `values` (already centred)."""
    n = len(values)
    omega: FloatArray = values.T @ values / n
    for k in range(1, min(lags, n - 1) + 1):
        gamma = values[k:].T @ values[:-k] / n
        omega = omega + (1 - k / (lags + 1)) * (gamma + gamma.T)
    return omega


def _default_lags(n: int) -> int:
    lags: int = math.floor(4 * (n / 100) ** (2.0 / 9.0))
    return lags


def _clean(returns: npt.ArrayLike) -> FloatArray:
    r = np.asarray(returns, dtype=np.float64)
    if np.isnan(r).any():
        raise ValueError("returns must not contain missing values")
    return r
