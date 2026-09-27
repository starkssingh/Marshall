"""Evaluation of volatility forecasters on identical folds and targets (VOL-005).

`evaluate_forecasters` puts every forecaster through the **same** walk-forward folds on the same
periods frame and the same target, so the comparison is fair by construction:

1. **Target**: the realized variance over the forecast horizon, ``y_t = rv_{t+1} + ... +
   rv_{t+h}``, with ``label_end`` the decision time of period t + h (for purging).
2. **Folds**: one `WalkForwardSplitter` on the periods' decision times, purged by that
   ``label_end``. In each fold every model is built fresh, fitted on the training periods only and
   asked for forecasts on the periods up to the fold's end; its test rows are kept. Rows where any
   model has no forecast, or the target is unknown, are dropped for every model (and counted), so
   all models are scored on exactly the same rows; fold-level scores are kept.
3. **Losses**: QLIKE (primary; robust to a noisy realized proxy, Patton 2011) and squared error
   on variance.
4. **Mincer-Zarnowitz**: ``y = a + b f + e`` by OLS with Newey-West standard errors
   (h - 1 lags for overlapping targets; White's for h = 1); the Wald test of ``a = 0, b = 1`` is
   chi-squared with 2 degrees of freedom.
5. **Diebold-Mariano** on QLIKE losses (h - 1 autocovariances): every model against the
   reference (``har``) two-sided, and one-sided against the reference and the default
   (``ewma_0.94``, the selection's fallback).
6. **Model Confidence Set** at ``1 - mcs_alpha`` (90 %) on QLIKE losses with the stationary
   bootstrap (Hansen, Lunde and Nason 2011).
7. **Breakdown** of mean QLIKE by session (labels the caller passes, known in advance) and by
   volatility regime: the trailing mean RV over ``regime_window`` periods at each decision, cut at
   quantiles computed on **each fold's training rows only**.
8. **Trials**: inside an experiment run each model evaluated on the test folds is one trial of
   the ``volatility_models`` family, never of a trading-strategy family (ADR 0046).

Sprint 6 is build-only: this runs on simulated periods only; no volatility board exists for real
data and nothing is promoted (ADR 0044).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.stats import chi2

from xq.core.config import VolatilityConfig, VolEvaluationConfig
from xq.core.seeds import derive_seed
from xq.core.types import Timeframe
from xq.models.volatility import VolForecaster, check_periods, realized_target
from xq.research.volatility.benchmarks import Deseasonalized, benchmark_forecasters
from xq.research.volatility.garch import garch_forecasters
from xq.tracking.registry import VOLATILITY_MODEL_FAMILY
from xq.validation.forecast_eval import (
    ModelConfidenceSet,
    diebold_mariano,
    diebold_mariano_less,
    loss_series,
    model_confidence_set,
)
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter

if TYPE_CHECKING:
    from xq.tracking.runs import RunContext

FloatArray = npt.NDArray[np.float64]
Factory = Callable[[], VolForecaster]
#: The code version of the evaluation (part of every trial configuration).
CODE_VERSION = 1


@dataclass(frozen=True)
class VolBoard:
    """The volatility board of one horizon: forecasts, losses, metrics, MCS and slices."""

    horizon: int
    forecasts: pd.DataFrame
    losses: pd.DataFrame
    metrics: pd.DataFrame
    fold_metrics: pd.DataFrame
    slices: pd.DataFrame
    mcs: ModelConfidenceSet
    fold_ids: list[str]
    n_dropped: int
    reference: str
    default: str


def board_forecasters(cfg: VolatilityConfig, period: Timeframe, *, seed: int) -> dict[str, Factory]:
    """The volatility board: the VOL-003 benchmarks and the VOL-004 GARCH family by name.

    On hourly periods every model is deseasonalized with the train-only diurnal factor.
    """
    garch = garch_forecasters(cfg.garch, seed=seed)
    if period == Timeframe("1h"):
        garch = {name: partial(_deseasonalized, make, cfg) for name, make in garch.items()}
    return {**benchmark_forecasters(cfg, period), **garch}


def _deseasonalized(make: Factory, cfg: VolatilityConfig) -> VolForecaster:
    return Deseasonalized(make(), cfg.realized.diurnal)


def mincer_zarnowitz(
    actual: npt.ArrayLike, forecast: npt.ArrayLike, *, lags: int
) -> dict[str, float]:
    """OLS of `actual` on `forecast` with Newey-West errors and the Wald test of a = 0, b = 1."""
    y = np.asarray(actual, dtype=np.float64)
    f = np.asarray(forecast, dtype=np.float64)
    n = len(y)
    if n < 10:
        return dict.fromkeys(("mz_alpha", "mz_beta", "mz_r2", "mz_wald", "mz_p"), math.nan)
    x = np.column_stack([np.ones(n), f])
    xtx_inv = np.linalg.inv(x.T @ x)
    coef = xtx_inv @ x.T @ y
    residual = y - x @ coef
    scores = x * residual[:, None]
    meat = scores.T @ scores
    for k in range(1, lags + 1):
        weight = 1 - k / (lags + 1)
        gamma = scores[k:].T @ scores[:-k]
        meat += weight * (gamma + gamma.T)
    covariance = xtx_inv @ meat @ xtx_inv
    theta = coef - np.array([0.0, 1.0])
    wald = float(theta @ np.linalg.solve(covariance, theta))
    total = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - float(residual @ residual) / total if total > 0 else math.nan
    return {
        "mz_alpha": float(coef[0]),
        "mz_beta": float(coef[1]),
        "mz_r2": r2,
        "mz_wald": wald,
        "mz_p": float(chi2.sf(wald, df=2)),
    }


def regime_labels(
    periods: pd.DataFrame, train_idx: npt.NDArray[np.int64], window: int, quantiles: list[float]
) -> npt.NDArray[np.object_]:
    """Volatility-regime label of every row, with cut-offs from the training rows only."""
    trailing = (
        pd.Series(periods["rv"].to_numpy(np.float64))
        .rolling(window, min_periods=window)
        .mean()
        .to_numpy()
    )
    reference = trailing[train_idx]
    reference = reference[np.isfinite(reference)]
    labels = np.full(len(trailing), None, dtype=object)
    if len(reference) == 0:
        return labels
    cuts = np.quantile(reference, quantiles)
    known = np.isfinite(trailing)
    codes = np.searchsorted(cuts, trailing[known], side="right")
    labels[known] = [f"v{c}" for c in codes]
    return labels


def evaluate_forecasters(
    periods: pd.DataFrame,
    factories: Mapping[str, Factory],
    horizon: int,
    splitter: WalkForwardConfig,
    cfg: VolEvaluationConfig,
    *,
    default: str,
    seed: int,
    sessions: pd.Series | None = None,
    run: RunContext | None = None,
) -> VolBoard:
    """Evaluate every forecaster on identical folds and targets (module docstring).

    Args:
        periods: The periods frame (`xq.research.volatility.realized`).
        factories: A fresh forecaster per call, by board name; must include the reference
            (``cfg.dm_reference``) and `default`.
        sessions: Optional session label per period (known in advance), for the breakdown.
        run: When given, each model is recorded as one trial on test folds of the volatility-model
            family ``volatility_models`` — never of a trading-strategy family, whatever the run's
            hypothesis (ADR 0046).

    Raises:
        ValueError: if the reference or default is missing, no fold exists, or a forecast on a
            scored row is not positive.
    """
    check_periods(periods)
    for required in (cfg.dm_reference, default):
        if required not in factories:
            raise ValueError(f"the board needs {required!r} (reference or default)")
    target, label_end = realized_target(periods, horizon)
    folds = WalkForwardSplitter(splitter).split(pd.DatetimeIndex(periods.index), label_end)
    if not folds:
        raise ValueError("the walk-forward splitter gives no fold on these periods")
    names = list(factories)
    frames = []
    for fold in folds:
        train = periods.iloc[fold.train_idx]
        upto = periods.iloc[: int(fold.test_idx[-1]) + 1]
        columns: dict[str, Any] = {
            "fold_id": fold.fold_id,
            "target": target.iloc[fold.test_idx].to_numpy(),
            "vol_regime": regime_labels(
                periods, fold.train_idx, cfg.regime_window, cfg.regime_quantiles
            )[fold.test_idx],
        }
        for name in names:
            model = factories[name]().fit(train)
            forecast = model.predict_variance(upto, horizon).to_numpy(np.float64)
            columns[name] = forecast[fold.test_idx]
        frames.append(pd.DataFrame(columns, index=periods.index[fold.test_idx]))
    stacked = pd.concat(frames)
    if sessions is not None:
        stacked.insert(2, "session", sessions.reindex(stacked.index).to_numpy())
    usable = stacked["target"].notna() & stacked[names].notna().all(axis=1)
    forecasts = stacked.loc[usable]
    values = forecasts[names].to_numpy(np.float64)
    if np.any(values <= 0):
        bad = sorted({names[j] for j in np.flatnonzero((values <= 0).any(axis=0))})
        raise ValueError(f"non-positive variance forecasts from {bad}")
    y = forecasts["target"]
    losses = pd.DataFrame(
        {name: loss_series(y, forecasts[name], "qlike").to_numpy() for name in names},
        index=forecasts.index,
    )
    squared = pd.DataFrame(
        {name: loss_series(y, forecasts[name], "squared").to_numpy() for name in names},
        index=forecasts.index,
    )
    mcs = model_confidence_set(
        losses,
        alpha=cfg.mcs_alpha,
        n_boot=cfg.mcs_n_boot,
        mean_block=cfg.mcs_mean_block,
        seed=derive_seed(seed, "vol_mcs", horizon),
    )
    rows = []
    for name in names:
        row: dict[str, Any] = {
            "model": name,
            "n": len(forecasts),
            "qlike": float(losses[name].mean()),
            "mse": float(squared[name].mean()),
            **mincer_zarnowitz(y, forecasts[name], lags=horizon - 1),
            "in_mcs": name in mcs.included,
            "mcs_p": mcs.p_values[name],
        }
        for label, other in (("ref", cfg.dm_reference), ("default", default)):
            if name == other:
                row[f"dm_vs_{label}_statistic"] = math.nan
                row[f"dm_vs_{label}_p"] = math.nan
                row[f"dm_vs_{label}_p_less"] = math.nan
                continue
            two = diebold_mariano(losses[name], losses[other], horizon=horizon)
            one = diebold_mariano_less(losses[name], losses[other], horizon=horizon)
            row[f"dm_vs_{label}_statistic"] = two.statistic
            row[f"dm_vs_{label}_p"] = two.p_value
            row[f"dm_vs_{label}_p_less"] = one.p_value
        rows.append(row)
    metrics = pd.DataFrame(rows)
    long = _long_losses(losses, forecasts, names)
    fold_metrics = (
        long.groupby(["fold_id", "model"], sort=False)["qlike"]
        .agg(qlike="mean", n="size")
        .reset_index()
    )
    slices = _slices(long)
    if run is not None:
        for name in names:
            run.record_trial(
                family_id=VOLATILITY_MODEL_FAMILY,
                config={
                    "board": "volatility",
                    "model": name,
                    "horizon": horizon,
                    "splitter": splitter.model_dump(mode="json"),
                    "evaluation": cfg.model_dump(mode="json"),
                    "code_version": CODE_VERSION,
                },
                evaluated_on_test=True,
            )
    return VolBoard(
        horizon=horizon,
        forecasts=forecasts,
        losses=losses,
        metrics=metrics,
        fold_metrics=fold_metrics,
        slices=slices,
        mcs=mcs,
        fold_ids=[f.fold_id for f in folds],
        n_dropped=int((~usable).sum()),
        reference=cfg.dm_reference,
        default=default,
    )


def _long_losses(losses: pd.DataFrame, forecasts: pd.DataFrame, names: list[str]) -> pd.DataFrame:
    """One row per (scored row, model) with its QLIKE, fold and slice labels."""
    labels = [c for c in ("fold_id", "session", "vol_regime") if c in forecasts]
    wide = losses.loc[:, names].copy()
    for column in labels:
        wide[column] = forecasts[column].to_numpy()
    return wide.melt(id_vars=labels, value_vars=names, var_name="model", value_name="qlike")


def _slices(long: pd.DataFrame) -> pd.DataFrame:
    """Mean QLIKE per slice kind, label and model (rows with a missing label are left out)."""
    frames = []
    for kind in ("session", "vol_regime"):
        if kind not in long:
            continue
        known = long.loc[long[kind].notna()]
        if known.empty:
            continue
        table = (
            known.assign(label=known[kind].astype(str))
            .groupby(["label", "model"], sort=True)["qlike"]
            .agg(qlike="mean", n="size")
            .reset_index()
        )
        table.insert(0, "slice", kind)
        frames.append(table)
    if not frames:
        return pd.DataFrame(columns=["slice", "label", "model", "qlike", "n"])
    return pd.concat(frames, ignore_index=True)
