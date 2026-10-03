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

p-values are ``(1 + #{bootstrap >= observed}) / (1 + B)``. The mean block length always follows
the gates' bootstrap convention (``conventions.bootstrap`` in ``config/gates.yaml``: Politis-White
on the family's average differential, at least ``min_block_days``); a caller cannot choose a block
(C-24, ADR 0055).

**Size on this sample.** Under strong serial dependence in a short sample both tests over-reject
even with the gates' block (AR(1) phi = 0.4, 400 periods, 8 strategies: SPA about 18 % and the
Reality Check about 15 % at a 10 % level; ADR 0055). `size_check` therefore measures the size on
the sample at hand: it fits an AR sieve to each strategy's demeaned differentials (Yule-Walker,
order by AIC), simulates null families with those dynamics (residual rows resampled together, so
the dependence across strategies is kept, every true mean zero: the least favourable null), runs the
tests on each and counts rejections at the gate's level. A gate result that uses SPA or the
Reality Check carries the warning "test over-rejects on this sample" when that rate exceeds
``warn_ratio`` (1.5) times the level.

**Size-adjusted p-value** (C-25, ADR 0058). The simulated null families also give each test's
null distribution of p-values on this sample. The size-adjusted p-value of an observed p is the
share of simulated null families whose p-value is at most p, ``(1 + #{p_null <= p}) / (1 +
n_sim)``: the probability, under nulls with this sample's dependence, of a result at least as
strong. When the size check flags over-rejection, the R2 SPA gate reads the size-adjusted p-value
instead of the raw one, at the same threshold (``spa_p_max``, 0.10); both are reported. The
threshold never moves: the p-value is corrected for the test's size on this sample.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import GateBootstrapConfig, GateCheck, GatesConfig, SpaSizeCheckConfig
from xq.core.seeds import derive_seed, make_rng
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
    #: Each strategy's own one-sided bootstrap p-value of a positive mean (studentized, from the
    #: same resamples): the raw p-values a per-family correction (VAL-006, Holm) adjusts.
    single_p: FloatArray

    def survivors(self, level: float) -> tuple[str, ...]:
        """Strategies whose Romano-Wolf adjusted p-value is at most `level`."""
        return tuple(name for name, p in zip(self.names, self.stepm_p, strict=True) if p <= level)

    def table(self) -> pd.DataFrame:
        """One row per strategy: mean differential, studentized statistic, adjusted p-value."""
        return pd.DataFrame(
            {
                "mean": self.means,
                "t_stat": self.t_stats,
                "p": self.single_p,
                "romano_wolf_p": self.stepm_p,
            },
            index=pd.Index(self.names, name="strategy"),
        )

    def gate_check(self, gates: GatesConfig, size: SizeCheck) -> GateCheck:
        """R2 ``spa_p_max`` on the consistent SPA p-value, with the size check's warning.

        The size check is required: no SPA gate result exists without it (C-24). When it flags
        over-rejection, the gate reads the size-adjusted p-value at the same threshold, and the
        raw one is named in the result (C-25; module docstring).
        """
        criterion = gates.criterion("R2", "spa_p_max")
        if not math.isclose(size.level, criterion.threshold):
            raise ValueError("the size check must run at the gate's level")
        warning = size.warning("spa")
        if warning is None:
            return criterion.check(self.spa_p)
        adjusted = size.adjusted_p("spa", self.spa_p)
        measure = f"{criterion.measure}, size-adjusted on this sample's simulated null"
        note = f"gated on the size-adjusted p-value; raw SPA p = {self.spa_p:.4g}"
        return dataclasses.replace(criterion, measure=measure).check(adjusted, (warning, note))


@dataclass(frozen=True)
class SizeCheck:
    """Rejection rates of SPA and the Reality Check on null families simulated with a sample's
    serial dependence (module docstring)."""

    level: float
    n_sim: int
    reality_check_size: float
    spa_size: float
    warn_ratio: float
    #: The AR order fitted to each strategy's differentials.
    ar_orders: tuple[int, ...]
    #: Each test's p-value on every simulated null family: the reference distribution of the
    #: size-adjusted p-value.
    reality_check_null_p: tuple[float, ...] = ()
    spa_null_p: tuple[float, ...] = ()

    def over_rejects(self, test: Literal["spa", "reality_check"]) -> bool:
        """Whether `test`'s simulated size exceeds ``warn_ratio`` times the level."""
        size = self.spa_size if test == "spa" else self.reality_check_size
        return size > self.warn_ratio * self.level

    def adjusted_p(self, test: Literal["spa", "reality_check"], p_value: float) -> float:
        """The size-adjusted p-value of an observed `p_value` of `test` (module docstring).

        Raises:
            ValueError: if the check kept no null p-values.
        """
        null = np.asarray(self.spa_null_p if test == "spa" else self.reality_check_null_p)
        if len(null) == 0:
            raise ValueError("the size check kept no null p-values")
        return float((1 + np.sum(null <= p_value)) / (1 + len(null)))

    def warning(self, test: Literal["spa", "reality_check"]) -> str | None:
        """The warning a gate result using `test` carries, or None."""
        if not self.over_rejects(test):
            return None
        size = self.spa_size if test == "spa" else self.reality_check_size
        name = "SPA" if test == "spa" else "Reality Check"
        return (
            f"test over-rejects on this sample: {name} rejects {size:.1%} of {self.n_sim} null "
            f"families simulated with this sample's serial dependence at the {self.level:.0%} "
            f"level (more than {self.warn_ratio:g}x nominal)"
        )


def family_tests(
    differentials: pd.DataFrame | npt.ArrayLike,
    *,
    bootstrap: GateBootstrapConfig,
    seed: int,
    n_boot: int | None = None,
) -> FamilyTest:
    """Reality Check, SPA and Romano-Wolf of a family (module docstring).

    Args:
        differentials: Periods x strategies; a DataFrame's columns name the strategies.
        bootstrap: The gates' bootstrap convention (``gates.conventions.bootstrap``): its block
            rule always sets the mean block length.
        seed: Seed of the resamples.
        n_boot: Bootstrap resamples (default: the convention's ``n_boot``).

    Raises:
        ValueError: for fewer than two periods, no strategy, or missing values.
    """
    names = (
        tuple(str(c) for c in differentials.columns)
        if isinstance(differentials, pd.DataFrame)
        else None
    )
    d = _matrix(differentials)
    n, k = d.shape
    names = names or tuple(f"s{i}" for i in range(k))
    block = gate_block_length(d.mean(axis=1), bootstrap.block_length, bootstrap.min_block_days)
    n_boot = bootstrap.n_boot if n_boot is None else n_boot
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
    single = np.array([_p_value(centred[:, j] / omega[j], float(t_stats[j])) for j in range(k)])
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
        single_p=single,
    )


def size_check(
    differentials: pd.DataFrame | npt.ArrayLike,
    *,
    bootstrap: GateBootstrapConfig,
    settings: SpaSizeCheckConfig,
    level: float,
    seed: int,
) -> SizeCheck:
    """Size of SPA and the Reality Check at `level` on nulls with this sample's dependence.

    Each strategy's demeaned differentials get an AR sieve (Yule-Walker, order 0 ...
    ``max_ar_order`` by AIC). A simulated family resamples the rows of the residuals together
    (keeping the dependence across strategies), runs them through the fitted recursions after a
    burn-in, so every true mean is zero, the least favourable null. The tests then run on it
    under the gates' block convention with ``settings.n_boot`` resamples.

    Raises:
        ValueError: as `family_tests`, or for a level outside (0, 1).
    """
    if not 0 < level < 1:
        raise ValueError("level must lie in (0, 1)")
    d = _matrix(differentials)
    n, k = d.shape
    centred = d - d.mean(axis=0)
    max_order = min(settings.max_ar_order, max(0, n // 10))
    fits = [_yule_walker_aic(centred[:, j], max_order) for j in range(k)]
    residuals = np.column_stack(
        [_residuals(centred[:, j], coefs, max_order) for j, coefs in enumerate(fits)]
    )
    residuals -= residuals.mean(axis=0)
    burn = 100 + 10 * max_order
    rng = make_rng(seed)
    rc_null: list[float] = []
    spa_null: list[float] = []
    for sim in range(settings.n_sim):
        shocks = residuals[rng.integers(0, len(residuals), n + burn)]
        family = _simulate(fits, shocks)[burn:]  # every true mean zero: the null boundary
        result = family_tests(
            family,
            bootstrap=bootstrap,
            n_boot=settings.n_boot,
            seed=derive_seed(seed, "size_check", sim),
        )
        rc_null.append(result.reality_check_p)
        spa_null.append(result.spa_p)
    return SizeCheck(
        level=level,
        n_sim=settings.n_sim,
        reality_check_size=sum(p <= level for p in rc_null) / settings.n_sim,
        spa_size=sum(p <= level for p in spa_null) / settings.n_sim,
        warn_ratio=settings.warn_ratio,
        ar_orders=tuple(len(c) for c in fits),
        reality_check_null_p=tuple(rc_null),
        spa_null_p=tuple(spa_null),
    )


def _matrix(differentials: pd.DataFrame | npt.ArrayLike) -> FloatArray:
    d = np.asarray(differentials, dtype=np.float64)
    if d.ndim == 1:
        d = d[:, None]
    n, k = d.shape
    if n < 2 or k < 1:
        raise ValueError("the family needs at least two periods and one strategy")
    if not np.isfinite(d).all():
        raise ValueError("the differentials have missing or infinite values")
    return d


def _yule_walker_aic(x: FloatArray, max_order: int) -> FloatArray:
    """AR coefficients by Yule-Walker (Levinson-Durbin), the order minimizing AIC."""
    n = len(x)
    acv = np.array([float(x[j:] @ x[: n - j]) / n for j in range(max_order + 1)])
    if acv[0] <= 0:
        return np.zeros(0)
    best, best_aic = np.zeros(0), n * math.log(acv[0])
    phi = np.zeros(0)
    sigma2 = acv[0]
    for p in range(1, max_order + 1):
        kappa = (acv[p] - phi @ acv[1:p][::-1]) / sigma2
        phi = np.append(phi - kappa * phi[::-1], kappa)
        sigma2 *= 1 - kappa**2
        if sigma2 <= 0:
            break
        aic = n * math.log(sigma2) + 2 * p
        if aic < best_aic:
            best, best_aic = phi.copy(), aic
    return best


def _residuals(x: FloatArray, coefs: FloatArray, start: int) -> FloatArray:
    """AR residuals from `start` on (a common start keeps the strategies' rows aligned)."""
    fitted = np.zeros(len(x) - start)
    for lag, c in enumerate(coefs, start=1):
        fitted += c * x[start - lag : len(x) - lag]
    residual: FloatArray = x[start:] - fitted
    return residual


def _simulate(fits: list[FloatArray], shocks: FloatArray) -> FloatArray:
    """Each column of `shocks` run through its AR recursion, from zero."""
    order = max((len(c) for c in fits), default=0)
    if order == 0:
        return shocks.copy()
    coefs = np.zeros((order, len(fits)))
    for j, c in enumerate(fits):
        coefs[: len(c), j] = c
    x = np.zeros((len(shocks) + order, shocks.shape[1]))
    for t in range(len(shocks)):
        lags = x[t : t + order][::-1]  # x[t-1], x[t-2], ... in simulation time
        x[t + order] = np.sum(coefs * lags, axis=0) + shocks[t]
    simulated: FloatArray = x[order:]
    return simulated


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
