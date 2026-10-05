"""ML-003: the seeded search is reproducible, stays in its space and spends exactly its budget."""

import math
from collections.abc import Mapping
from typing import Any

import pytest

from helpers.pipeline import REPO
from xq.core.config import MlHpoConfig, SearchDimension, load_config
from xq.core.errors import ConfigError
from xq.models.hpo import optuna_search, search_space

HPO = load_config("research", config_dir=REPO / "config").ml_config().hpo


def bowl(params: Mapping[str, Any]) -> float:
    """A smooth objective with its minimum at C = 1 and l1_ratio = 0.3."""
    return (math.log10(params["C"])) ** 2 + (params["l1_ratio"] - 0.3) ** 2


def run(seed: int, n: int = 20) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    search = optuna_search(
        "logistic", HPO, seed=seed, n_trials=n, record=lambda c, loss: seen.append(dict(c))
    )
    return search(bowl, "f000"), seen


def test_a_seed_reproduces_every_configuration_and_the_choice() -> None:
    best, seen = run(7)
    again, seen_again = run(7)
    assert seen == seen_again
    assert best == again
    _, seen_other = run(8)
    assert seen_other != seen


def test_the_budget_is_spent_exactly_inside_the_space() -> None:
    best, seen = run(3, n=25)
    assert len(seen) == 25
    space = search_space(HPO, "logistic")
    for config in seen:
        assert space["C"].low <= config["C"] <= space["C"].high  # type: ignore[operator]
        assert 0.0 <= config["l1_ratio"] <= 1.0
        assert config["max_iter"] == 5000  # the fixed parameters ride along
        assert config["fold_id"] == "f000"
    losses = [bowl(c) for c in seen]
    assert bowl(best) == min(losses)  # the best evaluated configuration is returned


def test_integer_log_and_choice_dimensions() -> None:
    seen: list[dict[str, Any]] = []
    search = optuna_search(
        "random_forest", HPO, seed=1, n_trials=15, record=lambda c, loss: seen.append(dict(c))
    )
    search(lambda p: float(p["min_samples_leaf"]), "f001")
    for config in seen:
        assert config["max_depth"] in (2, 3, 4, 6)
        assert isinstance(config["min_samples_leaf"], int)
        assert 50 <= config["min_samples_leaf"] <= 500
        assert config["n_estimators"] == 300


def test_an_unusable_configuration_does_not_stop_the_search() -> None:
    seen: list[float] = []
    search = optuna_search(
        "logistic", HPO, seed=2, n_trials=5, record=lambda c, loss: seen.append(loss)
    )
    best = search(lambda p: math.inf if p["C"] > 1 else p["C"], "f002")
    assert len(seen) == 5
    assert best["C"] <= 1 or all(math.isinf(x) for x in seen)


def test_the_configuration_is_refused_when_incomplete() -> None:
    with pytest.raises(ConfigError, match="no search space"):
        optuna_search("magic", HPO, seed=1)
    with pytest.raises(ConfigError, match="budget"):
        optuna_search("logistic", HPO, seed=1, n_trials=0)
    with pytest.raises(ValueError, match="either low/high or choices"):
        SearchDimension(low=1.0, high=2.0, choices=[1])
    with pytest.raises(ValueError, match="low > 0"):
        SearchDimension(low=0.0, high=1.0, log=True)
    assert isinstance(HPO, MlHpoConfig)
    assert HPO.n_trials == 50  # the plan's default budget
