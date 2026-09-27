"""VAL-003: probability of backtest overfitting by CSCV — a hand-computed case, noise families
around 0.5, a graded genuine edge near 0, and a single-point optimum on noise failing the gate."""

import numpy as np
import pytest

from helpers.pipeline import REPO
from helpers.strategies import graded_family, noise_family, overfit_grid
from xq.core.config import load_config
from xq.validation.pbo import n_combinations, pbo_cscv

PBO_MAX = load_config("research", config_dir=REPO / "config").gates_config().r2_validated.pbo_max


def test_a_hand_computed_case() -> None:
    # two blocks; A earns 2 then 0, B earns 1 throughout (mean metric)
    matrix = np.array([[2.0, 1.0], [2.0, 1.0], [0.0, 1.0], [0.0, 1.0]])
    result = pbo_cscv(matrix, n_blocks=2, metric="mean")
    # in-sample block 1: A wins (2 > 1) and is last out of sample; block 2: B wins (1 > 0) and is
    # last out of sample: both winners rank 1 of 2, w = 1/3, logit ln(1/2) < 0
    assert result.n_combinations == 2
    np.testing.assert_allclose(result.logits, [np.log(0.5), np.log(0.5)])
    assert result.pbo == 1.0
    np.testing.assert_allclose(result.is_performance, [2.0, 1.0])
    np.testing.assert_allclose(result.oos_performance, [0.0, 1.0])
    assert result.probability_of_loss == 0.0


def test_sixteen_blocks_give_12870_combinations() -> None:
    assert n_combinations(16) == 12_870
    result = pbo_cscv(noise_family(320, 5, seed=1))
    assert result.n_combinations == len(result.logits) == 12_870
    assert result.n_blocks == 16


def test_noise_families_have_a_pbo_near_one_half() -> None:
    values = [pbo_cscv(noise_family(1600, 40, seed=seed)).pbo for seed in range(30)]
    assert np.mean(values) == pytest.approx(0.5, abs=0.06)
    assert min(values) > 0.1  # no noise family looks skilled
    # most noise families fail the gate: the in-sample winner is noise
    assert np.mean([v > PBO_MAX for v in values]) > 0.9


def test_a_graded_genuine_edge_passes() -> None:
    for seed in range(5):
        result = pbo_cscv(graded_family(1600, 20, seed=seed, top_sharpe=0.25))
        assert result.pbo <= PBO_MAX
        assert result.probability_of_loss < 0.05


def test_a_single_point_optimum_on_noise_fails() -> None:
    matrix, points = overfit_grid([float(a) for a in range(5, 15)], [1.0, 2.0, 3.0, 4.0, 5.0], 1600)
    best = int(np.argmax(matrix.mean(axis=0) / matrix.std(axis=0, ddof=1)))
    assert points[best]  # a single best point exists in sample ...
    result = pbo_cscv(matrix)
    assert result.pbo > PBO_MAX  # ... and CSCV says the selection is overfit
    summary = result.summary()
    assert summary["pbo"] == result.pbo
    assert summary["n_configurations"] == 50


def test_inputs_are_checked() -> None:
    with pytest.raises(ValueError, match="even"):
        pbo_cscv(noise_family(100, 3, seed=0), n_blocks=5)
    with pytest.raises(ValueError, match="two configurations"):
        pbo_cscv(noise_family(100, 1, seed=0))
    with pytest.raises(ValueError, match="cannot fill"):
        pbo_cscv(noise_family(10, 3, seed=0))
    bad = noise_family(100, 3, seed=0)
    bad[5, 1] = np.nan
    with pytest.raises(ValueError, match="missing"):
        pbo_cscv(bad)
