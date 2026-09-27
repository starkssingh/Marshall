"""ARMA models of 1-bar log returns and their walk-forward estimator (STAT-006, BASE-003).

**Model.** An ARMA(p, q) of 1-bar log returns r with mean mu,
``(r_t - mu) = sum_i phi_i (r_{t-i} - mu) + e_t + sum_j theta_j e_{t-j}``, which is an
ARIMA(p, 1, q) of log price with drift. It is estimated by exact Gaussian maximum likelihood
(statsmodels' state-space ARIMA, constant ``trend="c"``) on a fold's training returns only, which
are scaled to unit variance for the optimizer (the estimates are scaled back). ``max_p`` models
choose p in 1..max_p by AIC on the same training returns (never on test data).

**Forecasts** are causal by construction. With the fitted parameters fixed, the innovations are
filtered forward from zero initial values, ``e = A(L) x / Theta(L)`` (x = r - mu), so the value at t
uses returns up to t only; the forecast made at t of the next ``horizon`` bars' total return is
``sum_{k=1..h} (mu + xhat_{t+k|t})`` with the usual recursion (known values up to t, forecasts
after, future innovations zero). Missing returns count as the mean (zero deviation).

**Estimator.** `ArmaForecast` fits the walk-forward runner's estimator protocol (WF-002): its
features are the decision bar's ``open`` and ``close`` (the bar's own log return,
``log(close / open)``, known at the decision), it learns from the training rows' returns (not from
the target values), and predicts the sum over ``horizon_bars`` of the next bars' returns at each
test row, the filter starting at the fold's first test row (the rows before are not adjacent).
The study that compares these models with the random walk is `xq.research.stats.arima`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.signal import lfilter
from statsmodels.tsa.arima.model import ARIMA

from xq.core.config import ArmaSpec
from xq.core.errors import ConfigError
from xq.models.base import ModelConfig, ModelSpec
from xq.research.stats.results import captured_warnings

FloatArray = npt.NDArray[np.float64]
#: The code version of `ArmaForecast` (part of cache keys and trial configurations).
CODE_VERSION = 1
MIN_TRAIN = 50


@dataclass(frozen=True)
class ArmaFit:
    """Fitted ARMA(p, q) parameters of 1-bar log returns, in return units."""

    p: int
    q: int
    mu: float
    ar: tuple[float, ...]
    ma: tuple[float, ...]
    sigma2: float
    aic: float
    nobs: int
    se: Mapping[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def params(self) -> dict[str, float]:
        """``mu``, ``ar.L<i>``, ``ma.L<j>`` and ``sigma2``."""
        values = {"mu": self.mu, "sigma2": self.sigma2}
        values.update({f"ar.L{i}": v for i, v in enumerate(self.ar, start=1)})
        values.update({f"ma.L{j}": v for j, v in enumerate(self.ma, start=1)})
        return values


def fit_arma(returns: npt.ArrayLike, p: int, q: int = 0) -> ArmaFit:
    """Exact maximum-likelihood ARMA(p, q) with a constant on `returns` (finite values only).

    The returns are scaled to unit variance for the optimizer and the estimates scaled back.

    Raises:
        ValueError: with fewer than 50 finite returns or a constant series.
    """
    r = np.asarray(returns, dtype=np.float64)
    r = r[np.isfinite(r)]
    if len(r) < MIN_TRAIN:
        raise ValueError(f"{len(r)} returns are too few to fit an ARMA model ({MIN_TRAIN} needed)")
    scale = float(np.std(r))
    if scale == 0:
        raise ValueError("the returns are constant")
    notes: list[str] = []
    with captured_warnings(notes):
        result = ARIMA(r / scale, order=(p, 0, q), trend="c").fit()
    names = list(result.param_names)
    params = dict(zip(names, np.asarray(result.params, dtype=np.float64), strict=True))
    errors = dict(zip(names, np.asarray(result.bse, dtype=np.float64), strict=True))
    se = {"mu": errors["const"] * scale, "sigma2": errors["sigma2"] * scale**2}
    se.update({k: v for k, v in errors.items() if k.startswith(("ar.", "ma."))})
    return ArmaFit(
        p=p,
        q=q,
        mu=params["const"] * scale,
        ar=tuple(params[f"ar.L{i}"] for i in range(1, p + 1)),
        ma=tuple(params[f"ma.L{j}"] for j in range(1, q + 1)),
        sigma2=params["sigma2"] * scale**2,
        aic=float(result.aic),
        nobs=len(r),
        se=se,
        notes=tuple(notes),
    )


def select_ar(returns: npt.ArrayLike, max_p: int) -> ArmaFit:
    """The AR(p), p in 1..max_p, with the lowest AIC on `returns` (the first on ties)."""
    fits = [fit_arma(returns, p) for p in range(1, max_p + 1)]
    return min(fits, key=lambda f: f.aic)


def fit_spec(returns: npt.ArrayLike, spec: ArmaSpec) -> ArmaFit:
    """Fit a configured model: ARMA(p, q), or AR(p) chosen by AIC when `spec.max_p` is set."""
    if spec.max_p is not None:
        return select_ar(returns, spec.max_p)
    return fit_arma(returns, spec.p, spec.q)


def arma_forecasts(fit: ArmaFit, returns: npt.ArrayLike, horizon: int) -> FloatArray:
    """At every position t, the forecast of the sum of the next `horizon` returns given r_1..r_t.

    Causal by construction (module docstring); missing returns count as the mean.
    """
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    r = np.asarray(returns, dtype=np.float64)
    n = len(r)
    x = np.where(np.isfinite(r), r - fit.mu, 0.0)
    phi = np.asarray(fit.ar, dtype=np.float64)
    theta = np.asarray(fit.ma, dtype=np.float64)
    eps = lfilter(np.r_[1.0, -phi], np.r_[1.0, theta], x) if n else x
    # xhat[k] holds the k-step forecasts of x from every origin t (k = 0: the known value)
    xhat: dict[int, FloatArray] = {0: x}
    for k in range(1, horizon + 1):
        value = np.zeros(n)
        for i, coefficient in enumerate(phi, start=1):
            value += coefficient * (xhat[k - i] if k - i >= 0 else _lag(x, i - k))
        for j, coefficient in enumerate(theta, start=1):
            if j >= k:  # innovations at or before the origin are known; later ones are zero
                value += coefficient * _lag(eps, j - k)
        xhat[k] = value
    total = sum(xhat[k] for k in range(1, horizon + 1))
    return np.asarray(total + horizon * fit.mu, dtype=np.float64)


def _lag(values: FloatArray, lag: int) -> FloatArray:
    """`values` delayed by `lag` positions (zeros before the start)."""
    if lag == 0:
        return values
    out = np.zeros(len(values))
    out[lag:] = values[: len(values) - lag]
    return out


class ArmaForecast:
    """An ARMA model of the decision bars' returns for the walk-forward runner (module docstring).

    Parameters: ``horizon_bars`` (required), ``p``, ``q`` or ``max_p`` (as `ArmaSpec`), and the
    ``open`` / ``close`` feature columns (default ``open`` and ``close``).
    """

    def __init__(self, params: Mapping[str, Any]) -> None:
        try:
            self.horizon = int(params["horizon_bars"])
        except KeyError as exc:
            raise ConfigError("an ARMA forecast needs 'horizon_bars'") from exc
        self.open_column = str(params.get("open", "open"))
        self.close_column = str(params.get("close", "close"))
        self.spec = ArmaSpec(
            p=int(params.get("p", 0)),
            q=int(params.get("q", 0)),
            max_p=None if params.get("max_p") is None else int(params["max_p"]),
        )
        self.fitted: ArmaFit | None = None

    def fit(self, x: pd.DataFrame, y: pd.Series, rng: np.random.Generator) -> None:
        """Fit on the training rows' bar returns (the targets are not used)."""
        self.fitted = fit_spec(self._returns(x), self.spec)

    def predict(self, x: pd.DataFrame) -> FloatArray:
        """The forecast of the next `horizon_bars` returns' sum at every row of `x`."""
        if self.fitted is None:
            raise RuntimeError("fit the ARMA forecast before predicting")
        return arma_forecasts(self.fitted, self._returns(x), self.horizon)

    def _returns(self, x: pd.DataFrame) -> FloatArray:
        opened = x[self.open_column].to_numpy(np.float64)
        closed = x[self.close_column].to_numpy(np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            values: FloatArray = np.log(closed / opened)
        return values


def _arma(params: Mapping[str, Any]) -> ArmaForecast:
    return ArmaForecast(params)


ARMA_SPEC = ModelSpec("arma", CODE_VERSION, "regression", _arma)


def arma_model_config(name: str, spec: ArmaSpec, horizon_bars: int) -> ModelConfig:
    """The walk-forward configuration of a configured ARMA model at one horizon."""
    params: dict[str, Any] = {"horizon_bars": horizon_bars, "open": "open", "close": "close"}
    if spec.max_p is not None:
        params["max_p"] = spec.max_p
    else:
        params.update(p=spec.p, q=spec.q)
    return ModelConfig(name=name, params=params, features=["open", "close"])
