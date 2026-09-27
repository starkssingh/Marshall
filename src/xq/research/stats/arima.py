"""AR, ARMA and ARIMA forecasts in walk-forward, compared with the random walk (STAT-006).

**Model.** An ARMA(p, q) of 1-bar log returns r with mean mu,
``(r_t - mu) = sum_i phi_i (r_{t-i} - mu) + e_t + sum_j theta_j e_{t-j}``, which is an
ARIMA(p, 1, q) of log price with drift. It is estimated by exact Gaussian maximum likelihood
(statsmodels' state-space ARIMA, constant ``trend="c"``) on a fold's training returns only.
``max_p`` models choose p in 1..max_p by AIC on the same training returns (never on test data).
SARIMA is not built: the plan allows it only if EDA-004 finds a stable daily cycle.

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

**Study.** `arma_study` runs every configured model and benchmark (``zero_return``,
``random_walk``) through `walk_forward` on identical folds per horizon (one splitter, one
``label_end``), and compares each model with each benchmark by a Diebold-Mariano test on squared
errors (Harvey correction, ``horizon`` lags), two-sided and one-sided (the model has the lower
expected loss). One-sided p-values are Holm-adjusted across horizons for each (model, benchmark)
pair; **useful evidence** (plan) is a Holm-adjusted p below ``alpha`` against every benchmark —
anything else, including in-sample significance of coefficients, is recorded and never promoted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.signal import lfilter
from statsmodels.tsa.arima.model import ARIMA

from xq.core.config import ArmaSpec
from xq.core.errors import ConfigError
from xq.core.seeds import derive_seed
from xq.models.base import ModelConfig, ModelSpec
from xq.models.baselines import forecast_baseline
from xq.research.stats.results import captured_warnings, holm_adjust
from xq.validation.forecast_eval import diebold_mariano, diebold_mariano_less, loss_series
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter
from xq.validation.walkforward import walk_forward

if TYPE_CHECKING:
    from xq.tracking.runs import RunContext

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


# --- the walk-forward estimator ------------------------------------------------------------------


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


# --- the study -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HorizonTarget:
    """One forecast horizon of the study: its target values and how to purge them.

    Args:
        label: The horizon's name (``"1h"``).
        y: Target values (the forward log return over the horizon) indexed by decision time.
        label_end: Each target's ``label_end`` on the same index.
        horizon_bars: The horizon in base bars (the ARMA forecast sums this many bars).
        random_walk: The ``open`` / ``close`` feature columns of the bar BASE-001's
            ``random_walk`` repeats, or None where the features lack them (then the random walk
            is not applicable at this horizon).
    """

    label: str
    y: pd.Series
    label_end: pd.Series
    horizon_bars: int
    random_walk: tuple[str, str] | None = None


@dataclass(frozen=True)
class ArmaStudy:
    """Out-of-sample comparisons of every model with every benchmark, per horizon."""

    comparisons: pd.DataFrame
    predictions: dict[str, pd.DataFrame]
    fold_ids: dict[str, list[str]]
    in_sample: dict[str, ArmaFit]

    def useful(self, model: str) -> bool:
        """Useful evidence: at some horizon the model beats every benchmark after Holm."""
        rows = self.comparisons.loc[self.comparisons["model"] == model]
        if rows.empty:
            return False
        per_horizon = rows.groupby("horizon")["useful"].all()
        return bool(per_horizon.any())


def arma_study(
    features: pd.DataFrame,
    horizons: Sequence[HorizonTarget],
    models: Mapping[str, ArmaSpec],
    benchmarks: Sequence[str],
    splitter: WalkForwardConfig,
    *,
    alpha: float,
    seed: int,
    run: RunContext | None = None,
    family_id: str | None = None,
) -> ArmaStudy:
    """Walk-forward ARMA forecasts against the benchmarks (module docstring).

    Args:
        features: Decision-time-indexed features with the decision bar's ``open`` and ``close``
            (and the random walk's columns where they exist).
        run: When given, every (model, horizon) evaluated on test folds is recorded as a trial of
            `family_id` (the benchmarks are references, not trials).

    Raises:
        ValueError: if the models' folds differ (they cannot: one splitter, one ``label_end``).
    """
    if run is not None and family_id is None:
        raise ValueError("recording trials needs the hypothesis family")
    rows: list[dict[str, Any]] = []
    predictions: dict[str, pd.DataFrame] = {}
    fold_ids: dict[str, list[str]] = {}
    in_sample: dict[str, ArmaFit] = {}
    for target in horizons:
        runs: dict[str, tuple[ModelSpec, ModelConfig]] = {
            name: (ARMA_SPEC, arma_model_config(name, spec, target.horizon_bars))
            for name, spec in models.items()
        }
        for name in benchmarks:
            if name == "random_walk":
                if target.random_walk is None:
                    continue  # no bar of the horizon in the features: not applicable
                opened, closed = target.random_walk
                config = ModelConfig(
                    name=name, params={"open": opened, "close": closed}, features=[opened, closed]
                )
            else:
                config = ModelConfig(name=name)
            runs[name] = (forecast_baseline(name), config)
        outputs = {
            name: walk_forward(
                features,
                target.y,
                target.label_end,
                spec,
                config,
                splitter,
                seed=derive_seed(seed, "arma_study", target.label, name),
            )
            for name, (spec, config) in runs.items()
        }
        ids = {name: [f.fold_id for f in out.folds] for name, out in outputs.items()}
        tests = {name: list(out.predictions.index) for name, out in outputs.items()}
        first = next(iter(outputs))
        if any(v != ids[first] for v in ids.values()) or any(
            v != tests[first] for v in tests.values()
        ):
            raise ValueError(f"models were evaluated on different folds at {target.label}")
        fold_ids[target.label] = ids[first]
        frame = pd.DataFrame(
            {name: out.predictions["y_pred"] for name, out in outputs.items()},
        )
        frame.insert(0, "y_true", outputs[first].predictions["y_true"])
        frame.insert(0, "fold_id", outputs[first].predictions["fold_id"])
        predictions[target.label] = frame
        for model in models:
            if run is not None and family_id is not None:
                run.record_trial(
                    family_id=family_id,
                    config={
                        "study": "arma",
                        "model": model,
                        "spec": models[model].model_dump(mode="json"),
                        "horizon": target.label,
                        "horizon_bars": target.horizon_bars,
                        "splitter": splitter.model_dump(mode="json"),
                        "code_version": CODE_VERSION,
                    },
                    evaluated_on_test=True,
                )
            for benchmark in benchmarks:
                if benchmark not in frame:
                    continue
                rows.append(_compare(frame, model, benchmark, target))
        if target is horizons[0]:
            for model, spec in models.items():
                train = _first_training_rows(features, target, splitter)
                if train is not None:
                    in_sample[model] = fit_spec(train, spec)
    comparisons = pd.DataFrame(rows)
    if not comparisons.empty:
        comparisons["p_holm"] = math.nan
        for _, group in comparisons.groupby(["model", "benchmark"]):
            adjusted = holm_adjust(group["p_one_sided"].to_numpy(np.float64))
            comparisons.loc[group.index, "p_holm"] = adjusted
        comparisons["useful"] = comparisons["p_holm"] < alpha
    return ArmaStudy(comparisons, predictions, fold_ids, in_sample)


def _compare(
    frame: pd.DataFrame, model: str, benchmark: str, target: HorizonTarget
) -> dict[str, Any]:
    rows = frame.loc[:, ["y_true", model, benchmark]].dropna()
    y = rows["y_true"]
    mine = loss_series(y, rows[model], "squared")
    theirs = loss_series(y, rows[benchmark], "squared")
    two_sided = diebold_mariano(mine, theirs, horizon=target.horizon_bars)
    one_sided = diebold_mariano_less(mine, theirs, horizon=target.horizon_bars)
    mse_model, mse_benchmark = float(mine.mean()), float(theirs.mean())
    return {
        "horizon": target.label,
        "horizon_bars": target.horizon_bars,
        "model": model,
        "benchmark": benchmark,
        "n": len(rows),
        "mse_model": mse_model,
        "mse_benchmark": mse_benchmark,
        "mse_reduction": 1 - mse_model / mse_benchmark if mse_benchmark > 0 else math.nan,
        "dm_statistic": two_sided.statistic,
        "p_two_sided": two_sided.p_value,
        "p_one_sided": one_sided.p_value,
    }


def _first_training_rows(
    features: pd.DataFrame, target: HorizonTarget, splitter: WalkForwardConfig
) -> FloatArray | None:
    """The bar returns of the first fold's training rows (for the in-sample record)."""
    folds = WalkForwardSplitter(splitter).split(pd.DatetimeIndex(features.index), target.label_end)
    if not folds:
        return None
    rows = features.iloc[folds[0].train_idx]
    with np.errstate(divide="ignore", invalid="ignore"):
        values: FloatArray = np.log(
            rows["close"].to_numpy(np.float64) / rows["open"].to_numpy(np.float64)
        )
    return values
