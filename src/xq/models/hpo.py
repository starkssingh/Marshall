"""Seeded hyperparameter search, and what counts as a trial (ML-003, C-34 (4)).

`optuna_search(family, cfg, seed=)` returns a `Search` for the in-fold pipeline (ML-002): for one
fold it runs Optuna's TPE sampler, **seeded** from the run seed, the family and the fold id, for
exactly ``n_trials`` configurations (the plan's fixed budget, 50 by default), drawing each from
the family's search space in ``config/ml.yaml`` (intervals, log-scaled or integer, or lists of
choices) on top of its fixed parameters. The objective is the pipeline's inner purged
cross-validation loss on the fold's fitting rows, so no configuration is ever scored on
validation or test rows; a family that stops early would do so on the inner validation rows
only. The best configuration (the first on ties) is returned as a `SearchResult` with the number
of configurations evaluated and the sampler's seed. The same seed gives the same configurations
in the same order and the same choice.

**What a trial is** (the owner's decision C-34 (4), superseding "every evaluated configuration is
a trial"): the deflated Sharpe ratio's trial count is the number of **distinct pipeline
specifications** evaluated on outer test folds: model family x feature set x target x target-set
version (`PipelineSpec`). `record_pipeline_trial` records a specification once per hypothesis
family, ``evaluated_on_test=True``; evaluating the same specification again (another run, other
hyperparameters or seeds) returns the trial already recorded. Inner search configurations are
**not** trials: `record_hpo` records them per fold (configurations evaluated, sampler seed,
chosen parameters and inner loss) as run metrics and an artifact, without touching the trial
counter.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import optuna
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from xq.core.config import MlHpoConfig, SearchDimension
from xq.core.errors import ConfigError
from xq.core.seeds import derive_seed
from xq.models.persistence import require_evidence
from xq.models.pipeline import Objective, PipelineOutput, Search, SearchResult
from xq.tracking.trials import find_family_trial, trial_config_hash

if TYPE_CHECKING:
    from xq.tracking.runs import RunContext

Recorder = Callable[[Mapping[str, Any], float], None]
#: What an infinite inner loss (no usable inner split) is reported to the sampler as.
_UNUSABLE = 1e300


def search_space(cfg: MlHpoConfig, family: str) -> dict[str, SearchDimension]:
    """The configured search space of `family`.

    Raises:
        ConfigError: if ``config/ml.yaml`` has none for it.
    """
    try:
        return dict(cfg.search_spaces[family])
    except KeyError:
        raise ConfigError(f"config/ml.yaml has no search space for {family!r}") from None


def suggest(trial: optuna.trial.Trial, space: Mapping[str, SearchDimension]) -> dict[str, Any]:
    """One configuration drawn from `space` by `trial`."""
    params: dict[str, Any] = {}
    for name, dim in space.items():
        if dim.choices is not None:
            params[name] = trial.suggest_categorical(name, dim.choices)
            continue
        low, high = float(dim.low or 0.0), float(dim.high or 0.0)  # both set: validated
        if dim.integer:
            params[name] = trial.suggest_int(name, int(low), int(high), log=dim.log)
        else:
            params[name] = trial.suggest_float(name, low, high, log=dim.log)
    return params


def optuna_search(
    family: str,
    cfg: MlHpoConfig,
    *,
    seed: int,
    record: Recorder | None = None,
    n_trials: int | None = None,
) -> Search:
    """A seeded TPE search over `family`'s configured space (module docstring).

    Args:
        record: Called with every evaluated configuration (fixed parameters included, with
            ``fold_id``) and its inner loss (an observer: configurations are not trials).
        n_trials: The budget (default: ``cfg.n_trials``).
    """
    space = search_space(cfg, family)
    fixed = dict(cfg.fixed.get(family, {}))
    budget = cfg.n_trials if n_trials is None else n_trials
    if budget < 1:
        raise ConfigError("a search needs a budget of at least one configuration")

    def search(objective: Objective, fold_id: str) -> SearchResult:
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        sampler_seed = derive_seed(seed, "hpo", family, fold_id)
        sampler = optuna.samplers.TPESampler(seed=sampler_seed)
        study = optuna.create_study(direction="minimize", sampler=sampler)

        def evaluate(trial: optuna.trial.Trial) -> float:
            params = {**fixed, **suggest(trial, space)}
            loss = objective(params)
            if record is not None:
                record({**params, "fold_id": fold_id}, loss)
            return loss if math.isfinite(loss) else _UNUSABLE

        study.optimize(evaluate, n_trials=budget)
        best = study.best_trial
        best_loss = float(best.value) if best.value is not None else math.inf
        return SearchResult(
            params={**fixed, **best.params},
            n_configs=len(study.trials),
            seed=sampler_seed,
            best_loss=best_loss if best_loss < _UNUSABLE else math.inf,
        )

    return search


class PipelineSpec(BaseModel):
    """A pipeline specification, the unit of the trial count (C-34 (4))."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The model family (``config/ml.yaml``), e.g. ``logistic``.
    model: str = Field(min_length=1)
    #: The feature set and its version, e.g. ``core.v1``.
    feature_set: str = Field(min_length=1)
    #: The target column, e.g. ``dir_h4``.
    target: str = Field(min_length=1)
    #: The target set and its version, e.g. ``fwd_returns.v1``.
    target_set_version: str = Field(min_length=1)

    def trial_config(self) -> dict[str, str]:
        """The configuration a trial of this specification is recorded (and deduplicated) by."""
        return {"kind": "ml_pipeline", **self.model_dump()}


def record_pipeline_trial(
    run: RunContext,
    family_id: str,
    spec: PipelineSpec,
    *,
    sharpe: float | None = None,
    returns: pd.Series | None = None,
    predictions: pd.DataFrame | None = None,
) -> str:
    """Count `spec`'s evaluation on outer test folds as a trial of `family_id`, once: if the
    family already has a trial of this specification, its id is returned and nothing is
    recorded.

    Raises:
        NotEvidenceError: if `predictions` (the evaluated outputs) came from a model loaded
            across library versions (C-34 (5)): a diagnostic is not evidence.
    """
    if predictions is not None:
        require_evidence(predictions)
    config = spec.trial_config()
    existing = find_family_trial(run.engine, family_id, trial_config_hash(config))
    if existing is not None:
        return existing
    return run.record_trial(
        family_id=family_id, config=config, evaluated_on_test=True, sharpe=sharpe, returns=returns
    )


def hpo_summary(output: PipelineOutput) -> list[dict[str, Any]]:
    """Per searched fold: its id, configurations evaluated, sampler seed, chosen parameters and
    their inner loss."""
    return [{"fold_id": f.fold_id, **f.hpo} for f in output.folds if f.hpo is not None]


def record_hpo(
    run: RunContext, output: PipelineOutput, spec: PipelineSpec, directory: Path
) -> Path:
    """Record the inner searches of `output` per fold, **not** as trials: the metrics
    ``hpo_n_configs``, ``hpo_seed`` and ``hpo_best_inner_loss`` per fold, and
    ``<directory>/hpo_<model>.json`` (the specification and every fold's summary) as an artifact
    of kind ``hpo``."""
    folds = hpo_summary(output)
    for fold in folds:
        run.log_metric("hpo_n_configs", float(fold["n_configs"]), fold_id=fold["fold_id"])
        run.log_metric("hpo_seed", float(fold["seed"]), fold_id=fold["fold_id"])
        if math.isfinite(fold["best_inner_loss"]):
            run.log_metric(
                "hpo_best_inner_loss", float(fold["best_inner_loss"]), fold_id=fold["fold_id"]
            )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"hpo_{spec.model}.json"
    payload = {"spec": spec.model_dump(), "folds": folds}
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    run.log_artifact(path, kind="hpo")
    return path
