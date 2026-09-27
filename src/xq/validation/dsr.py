"""Probabilistic and deflated Sharpe ratios (VAL-002).

In per-period units (VAL-001), for an observed Sharpe ratio ``sr`` over ``n`` periods with
skewness ``g3`` and non-excess kurtosis ``g4``:

- `probabilistic_sharpe` (PSR, Bailey and Lopez de Prado 2012): the probability that the true
  Sharpe ratio exceeds a benchmark ``sr*``,
  ``Phi((sr - sr*) sqrt(n - 1) / sqrt(1 - g3 sr + (g4 - 1) / 4 sr^2))``;
- `expected_max_sharpe`: the Sharpe ratio the best of ``N`` independent trials is expected to
  reach by luck alone, given the variance ``V`` of the trials' Sharpe ratios:
  ``sqrt(V) ((1 - gamma) Phi^-1(1 - 1/N) + gamma Phi^-1(1 - 1/(N e)))``, gamma = Euler-Mascheroni;
- `deflated_sharpe` (DSR, Bailey and Lopez de Prado 2014): the PSR against that expected maximum.

`deflated_sharpe_for_family` takes ``N`` and ``V`` from the trial registry (EXP-004): by default the
*effective* number of independent trials, and the variance of the recorded trial Sharpe ratios.
Trials record **annualized** Sharpe ratios of daily net returns, so that variance is divided by
``periods_per_year`` to get per-period units. With fewer than two trials there is nothing to
deflate against and the benchmark is 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from scipy.stats import norm
from sqlalchemy import Engine

from xq.core.config import AppConfig
from xq.tracking.trials import trial_count
from xq.validation.sharpe import moments, sharpe_ratio

EULER_GAMMA = 0.5772156649015329


@dataclass(frozen=True)
class DeflatedSharpe:
    """A deflated Sharpe ratio and what it was computed from (per-period units)."""

    dsr: float
    sharpe: float
    benchmark: float
    n: int
    skew: float
    kurtosis: float
    n_trials: float
    sharpe_variance: float


def probabilistic_sharpe(
    sr: float, n: int, skewness: float, kurt: float, sr_benchmark: float = 0.0
) -> float:
    """Probability that the true Sharpe ratio exceeds `sr_benchmark` (see the module docstring)."""
    if n < 2:
        return math.nan
    spread = 1 - skewness * sr + (kurt - 1) / 4 * sr**2
    if spread <= 0:
        return math.nan
    return float(norm.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(spread)))


def expected_max_sharpe(n_trials: float, sharpe_variance: float) -> float:
    """Expected maximum Sharpe ratio of `n_trials` independent trials under the null (no skill)."""
    if n_trials <= 1 or sharpe_variance <= 0:
        return 0.0
    first = float(norm.ppf(1 - 1 / n_trials))
    second = float(norm.ppf(1 - 1 / (n_trials * math.e)))
    return math.sqrt(sharpe_variance) * ((1 - EULER_GAMMA) * first + EULER_GAMMA * second)


def deflated_sharpe(
    sr: float,
    n: int,
    skewness: float,
    kurt: float,
    *,
    n_trials: float,
    sharpe_variance: float,
) -> DeflatedSharpe:
    """The PSR of `sr` against the expected maximum Sharpe ratio of the trials."""
    benchmark = expected_max_sharpe(n_trials, sharpe_variance)
    return DeflatedSharpe(
        dsr=probabilistic_sharpe(sr, n, skewness, kurt, benchmark),
        sharpe=sr,
        benchmark=benchmark,
        n=n,
        skew=skewness,
        kurtosis=kurt,
        n_trials=n_trials,
        sharpe_variance=sharpe_variance,
    )


def deflated_sharpe_of_returns(
    returns: npt.ArrayLike, *, n_trials: float, sharpe_variance: float
) -> DeflatedSharpe:
    """DSR of a per-period return series against `n_trials` trials of per-period variance."""
    r = np.asarray(returns, dtype=np.float64)
    skewness, kurt = moments(r)
    return deflated_sharpe(
        sharpe_ratio(r),
        len(r),
        skewness,
        kurt,
        n_trials=n_trials,
        sharpe_variance=sharpe_variance,
    )


def deflated_sharpe_for_family(
    cfg: AppConfig,
    engine: Engine,
    family_id: str,
    returns: npt.ArrayLike,
    *,
    periods_per_year: int,
    use_effective: bool = True,
) -> DeflatedSharpe:
    """DSR of daily `returns` with the trial count and Sharpe variance registered for `family_id`.

    Raises:
        ValueError: if the family has no registered trial.
    """
    stats = trial_count(cfg, engine, family_id)
    if stats.n_trials == 0:
        raise ValueError(f"no trials registered for family {family_id!r}")
    n_trials = float(stats.effective_n if use_effective else stats.n_trials)
    variance = (stats.sharpe_variance or 0.0) / periods_per_year
    return deflated_sharpe_of_returns(returns, n_trials=n_trials, sharpe_variance=variance)
