"""VAL-005: Diebold-Mariano, Giacomini-White and the Model Confidence Set — hand-computed values,
size on simulated nulls and power on simulated alternatives."""

import math
from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest
from scipy.signal import lfilter
from scipy.stats import t as student_t

from xq.validation.forecast_eval import diebold_mariano, giacomini_white, model_confidence_set

RNG = np.random.default_rng(31)


def rejection_rate(test: Callable[[], float], reps: int, level: float = 0.05) -> float:
    return float(np.mean([test() < level for _ in range(reps)]))


def test_diebold_mariano_by_hand() -> None:
    a = np.array([1.0, 3.0, 2.0, 5.0, 4.0, 6.0])
    b = np.zeros(6)
    d = a - b
    n = len(d)
    gamma0 = np.mean((d - d.mean()) ** 2)
    raw = d.mean() / math.sqrt(gamma0 / n)
    result = diebold_mariano(a, b, harvey=False)
    assert result.statistic == pytest.approx(raw)
    assert result.mean_difference == pytest.approx(3.5)
    corrected = diebold_mariano(a, b)
    assert corrected.statistic == pytest.approx(raw * math.sqrt((n - 1) / n))
    assert corrected.p_value == pytest.approx(2 * student_t.sf(corrected.statistic, n - 1))
    assert diebold_mariano(b, a).statistic == pytest.approx(-corrected.statistic)


@pytest.mark.parametrize(("horizon", "upper"), [(1, 0.07), (4, 0.10)])
def test_diebold_mariano_has_the_nominal_size(horizon: int, upper: float) -> None:
    n = 200

    def one() -> float:  # equal accuracy; multi-step losses are MA(h - 1)
        d = lfilter(np.ones(horizon), [1.0], RNG.normal(size=n + horizon))[horizon:]
        return diebold_mariano(d, np.zeros(n), horizon=horizon).p_value

    assert 0.02 <= rejection_rate(one, 2000) <= upper


def test_diebold_mariano_detects_a_better_forecast() -> None:
    def one() -> float:
        return diebold_mariano(RNG.normal(0.3, 1, 200), np.zeros(200)).p_value

    assert rejection_rate(one, 300) > 0.9


def test_giacomini_white_has_the_nominal_size_and_sees_conditional_differences() -> None:
    n = 300

    def null() -> float:
        return giacomini_white(RNG.normal(size=n), np.zeros(n)).p_value

    assert 0.02 <= rejection_rate(null, 1000) <= 0.08

    def predictable() -> float:
        # zero mean difference, but which forecast wins is predictable from the last difference
        d = lfilter([1.0], [1.0, -0.5], RNG.normal(size=n + 50))[50:]
        return giacomini_white(d, np.zeros(n)).p_value

    assert rejection_rate(predictable, 200) > 0.8


def test_giacomini_white_statistic_with_a_constant_instrument() -> None:
    d = np.array([0.5, -0.2, 0.9, 0.1, 0.4, -0.3, 0.8])
    result = giacomini_white(d, np.zeros(7), instruments=np.ones(7))
    z = d[1:]
    expected = len(z) * z.mean() ** 2 / np.mean(z**2)
    assert result.statistic == pytest.approx(expected)
    with pytest.raises(ValueError, match="instrument rows"):
        giacomini_white(d, np.zeros(7), instruments=np.ones(5))


def test_mcs_keeps_equal_models_and_drops_a_worse_one() -> None:
    n = 400

    def losses(worse: float) -> pd.DataFrame:
        values = RNG.normal(1.0, 1.0, size=(n, 4))
        values[:, 3] += worse
        return pd.DataFrame(values, columns=["a", "b", "c", "d"])

    kept_all = np.mean(
        [
            len(model_confidence_set(losses(0.0), n_boot=300, seed=i).included) == 4
            for i in range(100)
        ]
    )
    assert kept_all >= 0.85  # with alpha = 0.10 the true set is kept at least ~90 % of the time
    result = model_confidence_set(losses(0.5), n_boot=500, seed=7)
    assert result.eliminated[0] == "d"
    assert result.p_values["d"] < 0.10
    assert "d" not in result.included
    assert max(result.p_values.values()) == 1.0
    p_in_order = [result.p_values[name] for name in result.eliminated]
    assert p_in_order == sorted(p_in_order)  # MCS p-values never decrease along eliminations


def test_inputs_are_validated() -> None:
    with pytest.raises(ValueError, match="same length"):
        diebold_mariano(np.zeros(5), np.zeros(4))
    with pytest.raises(ValueError, match="missing"):
        diebold_mariano(np.array([1.0, np.nan, 2.0]), np.zeros(3))
    assert math.isnan(diebold_mariano(np.zeros(2), np.zeros(2)).p_value)
    with pytest.raises(ValueError, match="missing"):
        model_confidence_set(pd.DataFrame({"a": [1.0, np.nan], "b": [1.0, 2.0]}), seed=1)
    single = model_confidence_set(pd.DataFrame({"a": [1.0, 2.0, 3.0]}), seed=1)
    assert single.included == ["a"]
