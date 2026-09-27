"""VAL-006: Holm and Benjamini-Hochberg adjusted p-values — hand-computed reference values,
agreement with statsmodels' independent implementation, missing values, per-family adjustment,
and the error rates they control on simulated nulls."""

import numpy as np
import pandas as pd
import pytest
from statsmodels.stats.multitest import multipletests

from xq.research.stats.results import holm_adjust
from xq.validation.multiple_testing import (
    adjust,
    adjust_by_family,
    benjamini_hochberg,
    bonferroni,
    holm,
)

P = [0.01, 0.04, 0.03, 0.005, 0.20]


def test_hand_computed_reference_values() -> None:
    # sorted 0.005, 0.01, 0.03, 0.04, 0.20 (m = 5)
    # Holm: 5 x 0.005 = 0.025, 4 x 0.01 = 0.04, 3 x 0.03 = 0.09, 2 x 0.04 = 0.08 -> 0.09, 0.20
    np.testing.assert_allclose(holm(P), [0.04, 0.09, 0.09, 0.025, 0.20])
    # BH: 5/5 x 0.20 = 0.20, 5/4 x 0.04 = 0.05, 5/3 x 0.03 = 0.05, 5/2 x 0.01 = 0.025, 5 x 0.005
    np.testing.assert_allclose(benjamini_hochberg(P), [0.025, 0.05, 0.05, 0.025, 0.20])
    np.testing.assert_allclose(bonferroni(P), [0.05, 0.2, 0.15, 0.025, 1.0])


@pytest.mark.parametrize(("method", "reference"), [("holm", "holm"), ("bh", "fdr_bh")])
def test_the_adjustments_match_statsmodels(method: str, reference: str) -> None:
    rng = np.random.default_rng(0)
    for size in (1, 2, 7, 50):
        p = rng.uniform(0, 0.2, size)
        expected = multipletests(p, method=reference)[1]
        np.testing.assert_allclose(adjust(p, method), expected, rtol=1e-12)  # type: ignore[arg-type]


def test_missing_p_values_stay_missing_and_do_not_count() -> None:
    adjusted = holm([0.01, np.nan, 0.04])
    np.testing.assert_allclose(adjusted, [0.02, np.nan, 0.04])
    assert np.isnan(benjamini_hochberg([np.nan])).all()
    np.testing.assert_allclose(holm_adjust([0.01, np.nan, 0.04]), adjusted)  # the old name
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        holm([1.2])
    with pytest.raises(ValueError, match="unknown method"):
        adjust([0.1], "sidak")  # type: ignore[arg-type]


def test_each_family_is_adjusted_on_its_own_with_its_declared_method() -> None:
    tests = pd.DataFrame(
        {
            "family": ["board", "board", "board", "lags", "lags"],
            "test": ["a", "b", "c", "lag1", "lag5"],
            "p_value": [0.01, 0.03, 0.04, 0.02, 0.03],
        }
    )
    out = adjust_by_family(tests, methods={"lags": "bh"}, level=0.05)
    board = out.loc[out["family"] == "board"]
    lags = out.loc[out["family"] == "lags"]
    # Holm within the board: 3 x 0.01 = 0.03, 2 x 0.03 = 0.06, then 0.06
    np.testing.assert_allclose(board["adjusted_p"], [0.03, 0.06, 0.06])
    # BH within the lags: 2/1 x 0.02 = 0.04 -> 0.03, 2/2 x 0.03 = 0.03
    np.testing.assert_allclose(lags["adjusted_p"], [0.03, 0.03])
    assert list(out["method"]) == ["holm"] * 3 + ["bh"] * 2
    assert list(out["rejected"]) == [True, False, False, True, True]
    with pytest.raises(ValueError, match="lack columns"):
        adjust_by_family(tests.drop(columns="p_value"))


def test_holm_controls_the_family_wise_error_and_bh_the_false_discovery_rate() -> None:
    rng = np.random.default_rng(1)
    level, trials, m = 0.10, 2000, 20
    any_false_holm = any_false_raw = 0
    fdr = []
    for _ in range(trials):
        null = rng.uniform(size=m)  # 20 true nulls
        any_false_holm += bool((holm(null) <= level).any())
        any_false_raw += bool((null <= level).any())
        mixed = np.concatenate([rng.uniform(size=15), rng.uniform(0, 0.001, size=5)])
        rejected = benjamini_hochberg(mixed) <= level
        fdr.append(rejected[:15].sum() / max(rejected.sum(), 1))
    assert any_false_raw / trials > 0.8  # uncorrected: almost always a false rejection
    assert any_false_holm / trials <= level + 0.02
    assert np.mean(fdr) <= level + 0.02
