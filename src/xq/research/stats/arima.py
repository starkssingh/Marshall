"""Walk-forward ARMA forecasts compared with the random walk (STAT-006).

The ARMA model, its causal forecasts and its walk-forward estimator live in `xq.models.arma` (so the
baseline board can use them, BASE-003); they are re-exported here.

**Study.** `arma_study` runs every configured model and benchmark (``zero_return``,
``random_walk``) through `walk_forward` on identical folds per horizon (one splitter, one
``label_end``), and compares each model with each benchmark by a Diebold-Mariano test on squared
errors (Harvey correction, ``horizon`` lags), two-sided and one-sided (the model has the lower
expected loss). One-sided p-values are Holm-adjusted across horizons for each (model, benchmark)
pair; **useful evidence** (plan) is a Holm-adjusted p below ``alpha`` against every benchmark at
some horizon — anything else, including in-sample significance of coefficients, is recorded and
never promoted. SARIMA is not built: the plan allows it only if EDA-004 finds a stable daily cycle.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import ArmaSpec
from xq.core.seeds import derive_seed
from xq.models.arma import (
    ARMA_SPEC,
    CODE_VERSION,
    ArmaFit,
    ArmaForecast,
    arma_forecasts,
    arma_model_config,
    fit_arma,
    fit_spec,
    select_ar,
)
from xq.models.base import ModelConfig, ModelSpec
from xq.models.baselines import forecast_baseline
from xq.research.stats.results import holm_adjust
from xq.validation.forecast_eval import diebold_mariano, diebold_mariano_less, loss_series
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter
from xq.validation.walkforward import walk_forward

if TYPE_CHECKING:
    from xq.tracking.runs import RunContext

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "ARMA_SPEC",
    "ArmaFit",
    "ArmaForecast",
    "ArmaStudy",
    "HorizonTarget",
    "arma_forecasts",
    "arma_model_config",
    "arma_study",
    "fit_arma",
    "fit_spec",
    "select_ar",
]


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
