"""Seeded hyperparameter search with every configuration counted (ML-003).

`optuna_search(family, cfg, seed=, record=)` returns a `Search` for the in-fold pipeline
(ML-002): for one fold it runs Optuna's TPE sampler, **seeded** from the run seed, the family and
the fold id, for exactly ``n_trials`` configurations (the plan's fixed budget, 50 by default),
drawing each from the family's search space in ``config/ml.yaml`` (intervals, log-scaled or
integer, or lists of choices) on top of its fixed parameters. The objective is the pipeline's
inner purged cross-validation loss on the fold's fitting rows, so no configuration is ever scored
on validation or test rows; a family that stops early would do so on the inner validation rows
only. The best configuration (the first on ties) is returned.

**Every evaluated configuration is a trial.** `record(config, loss)` is called once per
configuration; `trial_recorder(run, family_id, context)` records each as a trial of the run's
hypothesis family, ``evaluated_on_test=False`` (EXP-004), so the trial counter and the deflated
Sharpe ratio see the whole search, not just its winner. The out-of-sample evaluation of the
chosen model is recorded separately by its caller, on test.

The same seed gives the same configurations in the same order and the same choice.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import optuna

from xq.core.config import MlHpoConfig, SearchDimension
from xq.core.errors import ConfigError
from xq.core.seeds import derive_seed
from xq.models.pipeline import Objective, Search

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
            ``fold_id``) and its inner loss.
        n_trials: The budget (default: ``cfg.n_trials``).
    """
    space = search_space(cfg, family)
    fixed = dict(cfg.fixed.get(family, {}))
    budget = cfg.n_trials if n_trials is None else n_trials
    if budget < 1:
        raise ConfigError("a search needs a budget of at least one configuration")

    def search(objective: Objective, fold_id: str) -> dict[str, Any]:
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        sampler = optuna.samplers.TPESampler(seed=derive_seed(seed, "hpo", family, fold_id))
        study = optuna.create_study(direction="minimize", sampler=sampler)

        def evaluate(trial: optuna.trial.Trial) -> float:
            params = {**fixed, **suggest(trial, space)}
            loss = objective(params)
            if record is not None:
                record({**params, "fold_id": fold_id}, loss)
            return loss if math.isfinite(loss) else _UNUSABLE

        study.optimize(evaluate, n_trials=budget)
        return {**fixed, **study.best_trial.params}

    return search


def trial_recorder(
    run: RunContext, family_id: str, context: Mapping[str, Any] | None = None
) -> Recorder:
    """Record every evaluated configuration as a trial of `family_id` in `run`, not on test."""

    def record(config: Mapping[str, Any], loss: float) -> None:
        run.record_trial(
            family_id=family_id,
            config={
                **dict(context or {}),
                "stage": "hpo",
                "params": {k: v for k, v in config.items() if k != "fold_id"},
                "fold_id": config.get("fold_id"),
                "inner_cv_loss": loss if math.isfinite(loss) else None,
            },
            evaluated_on_test=False,
        )

    return record
