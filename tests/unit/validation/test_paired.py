"""R1 against the best baseline: the paired block bootstrap of the Sharpe difference picks the
best baseline, keeps its size when the candidate adds nothing, and uses the pairing to detect a
small but consistent improvement."""

import numpy as np
import pandas as pd
import pytest

from helpers.quality import repo_config
from xq.validation.paired import beats_best_baseline

GATES = repo_config().gates_config()
BOOT = GATES.conventions.bootstrap


def frame(n: int, seed: int) -> tuple[pd.Series, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    index = pd.RangeIndex(n)
    base = pd.DataFrame(
        {
            "cash_like": rng.normal(0.0, 0.001, n),
            "buy_and_hold": rng.normal(0.0004, 0.01, n),
            "momentum": rng.normal(0.0002, 0.01, n),
        },
        index=index,
    )
    return pd.Series(rng.normal(0.0, 0.01, n), index=index), base


def test_the_best_baseline_is_the_one_with_the_highest_sharpe() -> None:
    candidate, baselines = frame(2000, 1)
    candidate = baselines["buy_and_hold"] + np.random.default_rng(2).normal(0.0008, 0.002, 2000)
    test = beats_best_baseline(
        candidate, baselines, bootstrap=BOOT, periods_per_year=252, seed=3, n_boot=999
    )
    sharpes = baselines.mean() / baselines.std() * np.sqrt(252)
    assert test.baseline == sharpes.idxmax()
    assert test.margin > 0
    p_check, margin_check = test.gate_checks(GATES)
    assert (p_check.criterion.key, margin_check.criterion.key) == (
        "best_baseline_p_max",
        "best_baseline_margin_sharpe",
    )
    assert p_check.passed
    assert margin_check.passed


def test_a_candidate_that_adds_nothing_is_rejected_at_about_the_nominal_rate() -> None:
    rejections = 0
    for seed in range(200):
        rng = np.random.default_rng(seed)
        common = rng.normal(0.0005, 0.01, 500)
        baseline = pd.DataFrame({"b": common + rng.normal(0, 0.003, 500)})
        candidate = pd.Series(common + rng.normal(0, 0.003, 500))  # the same edge, other noise
        test = beats_best_baseline(
            candidate, baseline, bootstrap=BOOT, periods_per_year=252, seed=seed, n_boot=199
        )
        rejections += test.p_value < 0.10
    assert 0.04 <= rejections / 200 <= 0.17  # 200 replications: standard error 2.1 points


def test_pairing_detects_a_small_consistent_improvement() -> None:
    rng = np.random.default_rng(5)
    common = rng.normal(0.0003, 0.01, 1500)
    baseline = pd.DataFrame({"b": common})
    candidate = pd.Series(common + rng.normal(0.0002, 0.0005, 1500))  # +2 bp a day, tightly paired
    test = beats_best_baseline(
        candidate, baseline, bootstrap=BOOT, periods_per_year=252, seed=6, n_boot=999
    )
    assert test.p_value < 0.01
    assert test.margin == pytest.approx(test.candidate_sharpe - test.baseline_sharpe)


def test_inputs_are_checked() -> None:
    candidate, baselines = frame(100, 7)
    with pytest.raises(ValueError, match="at least one baseline"):
        beats_best_baseline(
            candidate, baselines.iloc[:, :0], bootstrap=BOOT, periods_per_year=252, seed=1
        )
    with pytest.raises(ValueError, match="candidate's days"):
        beats_best_baseline(
            candidate, baselines.iloc[1:], bootstrap=BOOT, periods_per_year=252, seed=1
        )
