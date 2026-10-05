"""ML-001: every registered forecaster family conforms to the Forecaster protocol."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from xq.models.base import (
    Forecaster,
    ForecasterError,
    SklearnForecaster,
    build_forecaster,
    forecaster_spec,
)

FAMILIES = {
    "logistic": {"C": 0.5, "l1_ratio": 0.5},
    "ridge": {"alpha": 2.0},
    "random_forest": {"n_estimators": 20, "max_depth": 3, "min_samples_leaf": 5},
}


def data(n: int = 300, seed: int = 4) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    x = pd.DataFrame(rng.normal(size=(n, 3)), columns=["a", "b", "c"])
    signal = x["a"] - 0.5 * x["b"] + rng.normal(0, 0.5, n)
    return x, (signal > 0).astype(float), signal


@pytest.mark.parametrize("name", sorted(FAMILIES))
def test_every_family_conforms_to_the_protocol(name: str, tmp_path: Path) -> None:
    model: Forecaster = build_forecaster(name, FAMILIES[name], seed=7)
    x, classes, values = data()
    y = classes if model.task == "classification" else values
    weights = pd.Series(np.linspace(0.5, 1.5, len(x)))
    assert model.fit(x, y, sample_weight=weights, eval_set=(x.iloc[:50], y.iloc[:50])) is model
    pred = model.predict(x)
    assert pred.dtype == np.float64
    assert pred.shape == (len(x),)
    if model.task == "classification":
        p = model.predict_proba(x)
        assert ((p >= 0) & (p <= 1)).all()
        np.testing.assert_array_equal(pred, (p > 0.5).astype(float))
        assert np.mean(pred == y) > 0.7  # it learns the planted signal
    else:
        assert np.corrcoef(pred, y)[0, 1] > 0.7
        with pytest.raises(ForecasterError, match="no probabilities"):
            model.predict_proba(x)
    card = model.model_card()
    assert card["family"] == name
    assert card["features"] == ["a", "b", "c"]
    assert card["seed"] == 7
    assert card["code_version"] == forecaster_spec(name).code_version
    model.save(tmp_path / "model.joblib")
    again = SklearnForecaster.load(tmp_path / "model.joblib")
    np.testing.assert_array_equal(again.predict(x), pred)


@pytest.mark.parametrize("name", sorted(FAMILIES))
def test_a_fixed_seed_gives_identical_forecasts(name: str) -> None:
    x, classes, values = data()
    y = classes if forecaster_spec(name).task == "classification" else values
    one = build_forecaster(name, FAMILIES[name], seed=11).fit(x, y).predict(x)
    two = build_forecaster(name, FAMILIES[name], seed=11).fit(x, y).predict(x)
    np.testing.assert_array_equal(one, two)


def test_forecasters_refuse_what_they_cannot_use() -> None:
    x, classes, _ = data()
    model = build_forecaster("logistic", {}, seed=1)
    with pytest.raises(ForecasterError, match="not fitted"):
        model.predict_proba(x)
    with pytest.raises(ForecasterError, match="both classes"):
        model.fit(x, pd.Series(np.ones(len(x))))
    with pytest.raises(ForecasterError, match="missing"):
        model.fit(x.assign(a=np.nan), classes)
    with pytest.raises(ForecasterError, match="one known value"):
        model.fit(x, classes.iloc[:-1])
    with pytest.raises(ForecasterError, match="non-negative"):
        model.fit(x, classes, sample_weight=pd.Series(-np.ones(len(x))))
    model.fit(x, classes)
    with pytest.raises(ForecasterError, match="fitted on columns"):
        model.predict(x[["b", "a", "c"]])
    with pytest.raises(KeyError, match="unknown forecaster"):
        forecaster_spec("magic")
