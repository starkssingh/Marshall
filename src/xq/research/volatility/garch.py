"""GARCH-family volatility forecasters with `arch` (VOL-004).

`GarchForecaster` is a `VolForecaster` on a periods frame's ``ret`` (daily returns, or hourly
returns deseasonalized by wrapping it in `Deseasonalized`). The configured family
(``config/volatility.yaml`` ``garch``) is every process x every error distribution:

- ``garch``: GARCH(1,1); ``gjr``: GJR-GARCH(1,1) (o = 1, the leverage term); ``egarch``:
  EGARCH(1,1) with its asymmetry term;
- errors ``normal``, ``t`` (Student) and ``skewt`` (Hansen's skewed t); names read
  ``<process>_<distribution>``, e.g. ``gjr_skewt``.

**Fit** (per fold, on the training periods only): the returns are divided by their training
standard deviation (a scale fixed at fit time, so the optimizer sees unit variance) and the model
is estimated by maximum likelihood with a zero or constant mean. The fit's convergence flag,
the persistence (``alpha + beta + gamma / 2``; beta alone for EGARCH) and any library warnings are
kept in `diagnostics`.

**Forecast.** With the fitted parameters fixed, the conditional variance is filtered over the
periods passed and the variance of the next h periods' summed return is the sum of the h-step
forecasts, times the scale squared (a missing return counts as a zero shock). GARCH and GJR use
analytic multi-step forecasts; EGARCH has
them only one step ahead, so beyond it they are simulated (``simulations`` paths, from a
distribution seeded through `xq.core.seeds`). The recursion starts from arch's backcast of the
first 75 periods passed, so forecasts from the 75th period on use no later data; in walk-forward
the frame starts with the training data, so every test forecast is causal. FIGARCH is not built:
the plan allows it only if STAT-004 (Sprint 8) finds long memory in absolute returns.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from arch.univariate import (
    EGARCH,
    GARCH,
    ConstantMean,
    Normal,
    SkewStudent,
    StudentsT,
    ZeroMean,
)
from arch.univariate.distribution import Distribution

from xq.core.config import GarchConfig, GarchSpec
from xq.core.seeds import derive_seed, make_rng
from xq.models.volatility import VolForecaster
from xq.research.stats.results import captured_warnings

FloatArray = npt.NDArray[np.float64]
Factory = Callable[[], VolForecaster]
_DISTRIBUTIONS: dict[str, type[Distribution]] = {
    "normal": Normal,
    "t": StudentsT,
    "skewt": SkewStudent,
}
#: Origins forecast per arch call when simulating (bounds the simulation's memory).
_CHUNK = 64


@dataclass
class GarchDiagnostics:
    """What the fit reported: convergence, persistence and warnings."""

    converged: bool = False
    persistence: float = math.nan
    params: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class GarchForecaster(VolForecaster):
    """One GARCH-family model (module docstring)."""

    def __init__(
        self,
        name: str,
        spec: GarchSpec,
        distribution: str,
        *,
        mean: str = "zero",
        simulations: int = 1000,
        seed: int = 0,
    ) -> None:
        if distribution not in _DISTRIBUTIONS:
            raise ValueError(f"unknown error distribution {distribution!r}")
        self.name = name
        self.spec = spec
        self.distribution = distribution
        self.mean = mean
        self.simulations = simulations
        self.seed = seed
        self.scale = math.nan
        self.params: pd.Series | None = None
        self.diagnostics = GarchDiagnostics()

    def _model(self, returns: FloatArray, seed: int | None = None) -> Any:
        volatility = (
            GARCH(p=1, o=self.spec.o, q=1)
            if self.spec.vol == "GARCH"
            else EGARCH(p=1, o=self.spec.o, q=1)
        )
        errors = _DISTRIBUTIONS[self.distribution](
            seed=make_rng(self.seed if seed is None else seed)
        )
        mean_model = ZeroMean if self.mean == "zero" else ConstantMean
        return mean_model(returns, volatility=volatility, distribution=errors, rescale=False)

    def _fit(self, periods: pd.DataFrame) -> None:
        r = periods["ret"].to_numpy(np.float64)
        r = r[np.isfinite(r)]
        if len(r) < 100:
            raise ValueError(f"{self.name}: {len(r)} training returns are too few for a GARCH")
        self.scale = float(np.std(r))
        if self.scale == 0:
            raise ValueError(f"{self.name}: the training returns are constant")
        notes: list[str] = []
        with captured_warnings(notes):
            result = self._model(r / self.scale).fit(disp="off")
        self.params = result.params
        values = {str(k): float(v) for k, v in result.params.items()}
        if self.spec.vol == "GARCH":
            persistence = values["alpha[1]"] + values["beta[1]"] + values.get("gamma[1]", 0.0) / 2
        else:
            persistence = values["beta[1]"]
        self.diagnostics = GarchDiagnostics(
            converged=int(result.convergence_flag) == 0,
            persistence=persistence,
            params=values,
            notes=notes,
        )

    def _predict(self, periods: pd.DataFrame, horizon: int) -> FloatArray:
        if self.params is None:
            raise RuntimeError(f"fit {self.name} before predicting")
        r = np.nan_to_num(periods["ret"].to_numpy(np.float64), nan=0.0) / self.scale
        n = len(r)
        analytic = self.spec.vol == "GARCH" or horizon == 1
        out = np.full(n, np.nan)
        if n == 0:
            return out
        notes: list[str] = []
        with captured_warnings(notes):
            if analytic:
                fixed = self._model(r).fix(self.params)
                variance = fixed.forecast(horizon=horizon, start=0, reindex=False).variance
                out[:] = variance.to_numpy(np.float64).sum(axis=1)
            else:
                for begin in range(0, n, _CHUNK):
                    end = min(n, begin + _CHUNK)
                    chunk_seed = derive_seed(self.seed, "garch_simulation", begin)
                    fixed = self._model(r[:end], chunk_seed).fix(self.params)
                    variance = fixed.forecast(
                        horizon=horizon,
                        start=begin,
                        reindex=False,
                        method="simulation",
                        simulations=self.simulations,
                    ).variance
                    out[begin:end] = variance.to_numpy(np.float64).sum(axis=1)
        for note in notes:
            if note not in self.diagnostics.notes:
                self.diagnostics.notes.append(note)
        return out * self.scale**2


def _make(
    name: str, spec: GarchSpec, distribution: str, cfg: GarchConfig, seed: int
) -> VolForecaster:
    return GarchForecaster(
        name, spec, distribution, mean=cfg.mean, simulations=cfg.simulations, seed=seed
    )


def garch_forecasters(cfg: GarchConfig, *, seed: int) -> dict[str, Factory]:
    """Factories of every configured process x distribution, ``<process>_<distribution>``."""
    return {
        f"{name}_{dist}": partial(
            _make, f"{name}_{dist}", spec, dist, cfg, derive_seed(seed, "garch", name, dist)
        )
        for name, spec in cfg.models.items()
        for dist in cfg.distributions
    }
